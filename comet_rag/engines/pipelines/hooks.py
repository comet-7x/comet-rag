from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Protocol, Self, cast

from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.engines.documents.common.archive import ArchiveLimits
from comet_rag.engines.offload import run_sync
from comet_rag.engines.pipelines.types import PipelineConfig
from comet_rag.ports.document import ExtractedDocument, NormalizedDocument
from comet_rag.ports.source import LoadedResource
from comet_rag.ports.vision import AsyncVisionDescriptionPort, VisionDescriptionPort

LoaderContent = LoadedResource
ExtractHook = Callable[[LoaderContent, PipelineConfig], ExtractedDocument]
AsyncExtractHook = Callable[
    [LoaderContent, PipelineConfig], Awaitable[ExtractedDocument]
]
ChunkHook = Callable[[str, PipelineConfig], list[str]]
DocumentChunkHook = Callable[[NormalizedDocument, PipelineConfig], list[ChunkDraft]]


def adapt_legacy_chunk_hook(hook: ChunkHook) -> DocumentChunkHook:
    """把旧 ``str -> list[str]`` Hook 提升为文档级 Hook。

    旧返回值没有位置信息，不能用 ``str.find`` 猜测，故 span 明确留空。文档 metadata
    也不复制进 draft；统一合并由 Service 在 ChunkDraft 之后执行。
    """

    @wraps(hook)
    def adapted(
        document: NormalizedDocument, config: PipelineConfig
    ) -> list[ChunkDraft]:
        warnings.warn(
            "PipelineHooks.chunker(str, config) 已弃用；请迁移到 "
            "PipelineHooks.document_chunker(NormalizedDocument, config)。"
            "兼容入口最早在 0.3.0 移除。",
            DeprecationWarning,
            stacklevel=2,
        )
        texts = hook(document.markdown, config)
        if not isinstance(texts, list):
            raise TypeError("旧 ChunkHook 必须返回 list[str]")
        drafts: list[ChunkDraft] = []
        for ordinal, text in enumerate(texts):
            if not isinstance(text, str):
                raise TypeError(
                    f"旧 ChunkHook 第 {ordinal} 项必须是 str，收到 {type(text).__name__}"
                )
            if not text.strip():
                raise ValueError(f"旧 ChunkHook 第 {ordinal} 项不能是空白文本")
            drafts.append(ChunkDraft(text=text, ordinal=ordinal))
        return drafts

    return adapted


@dataclass(frozen=True, slots=True)
class HooksState[H]:
    """单一执行模型的注册表快照；函数对象保留身份。"""

    extractors: dict[str, H]
    chunkers: dict[str, ChunkHook]
    document_chunkers: dict[str, DocumentChunkHook] = field(default_factory=dict)


class ChunkHookProvider(Protocol):
    def get_document_chunker(self, file_type: str) -> DocumentChunkHook: ...


class HookProvider(ChunkHookProvider, Protocol):
    def extract(
        self, file_type: str, content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument: ...


class AsyncHookProvider(ChunkHookProvider, Protocol):
    async def extract(
        self, file_type: str, content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument: ...


class _Registry[H]:
    def __init__(self, state: HooksState[H] | None = None) -> None:
        self.restore(state or HooksState({}, {}))

    def snapshot(self) -> HooksState[H]:
        return HooksState(
            dict(self._extractors), dict(self._chunkers), dict(self._document_chunkers)
        )

    def restore(self, state: HooksState[H]) -> None:
        self._extractors = dict(state.extractors)
        self._chunkers = dict(state.chunkers)
        self._document_chunkers = dict(state.document_chunkers)

    def fork(self) -> Self:
        return type(self)(self.snapshot())

    @contextmanager
    def temporary(self) -> Iterator[None]:
        state = self.snapshot()
        try:
            yield
        finally:
            self.restore(state)

    def extractor(self, *file_types: str) -> Callable[[H], H]:
        def register(hook: H) -> H:
            for file_type in file_types:
                self._extractors[file_type.lower()] = hook
            return hook

        return register

    def get_extractor(self, file_type: str) -> H:
        try:
            return self._extractors[file_type.lower()]
        except KeyError:
            raise ValueError(
                f"No extractor registered for {file_type!r}. Registered: {sorted(self._extractors)}"
            ) from None

    def chunker(self, *file_types: str) -> Callable[[ChunkHook], ChunkHook]:
        def register(hook: ChunkHook) -> ChunkHook:
            for file_type in file_types:
                self._chunkers[file_type.lower()] = hook
            return hook

        return register

    def document_chunker(
        self, *file_types: str
    ) -> Callable[[DocumentChunkHook], DocumentChunkHook]:
        def register(hook: DocumentChunkHook) -> DocumentChunkHook:
            for file_type in file_types:
                self._document_chunkers[file_type.lower()] = hook
            return hook

        return register

    def get_chunker(self, file_type: str) -> ChunkHook:
        return self._chunkers.get(file_type.lower(), _default_chunk)

    def get_document_chunker(self, file_type: str) -> DocumentChunkHook:
        if chunker := self._document_chunkers.get(file_type.lower()):
            return chunker
        if legacy := self._chunkers.get(file_type.lower()):
            return adapt_legacy_chunk_hook(legacy)
        return _default_document_chunk


class HookRegistry(_Registry[ExtractHook]):
    def extract(
        self, file_type: str, content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        return self.get_extractor(file_type)(content, config)


class AsyncHookRegistry(_Registry[AsyncExtractHook]):
    async def extract(
        self, file_type: str, content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        return await self.get_extractor(file_type)(content, config)

    def sync_extractor(self, *file_types: str) -> Callable[[ExtractHook], ExtractHook]:
        """显式接入同步提取器；取消时先排干线程，再允许调用方释放文件。"""

        def register(hook: ExtractHook) -> ExtractHook:
            async def extract(
                content: LoaderContent, config: PipelineConfig
            ) -> ExtractedDocument:
                return await run_sync(hook, content, config)

            self.extractor(*file_types)(extract)
            return hook

        return register


# 默认注册表分开持有各自的 Hook；Context 从对应注册表 fork，禁止混用客户端。
PipelineHooks = HookRegistry()
AsyncPipelineHooks = AsyncHookRegistry()


def _docx_input(loader_content: LoaderContent) -> tuple[str, str]:
    file_type = str(loader_content.metadata.get("file_type", "docx")).lower()
    media_type = (
        "application/msword"
        if file_type == "doc"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    filename = loader_content.metadata.get("file_name")
    return (
        filename if isinstance(filename, str) else loader_content.path.name,
        media_type,
    )


@PipelineHooks.extractor("docx", "doc")
def _extract_docx(content: LoaderContent, config: PipelineConfig) -> ExtractedDocument:
    from comet_rag.engines.documents.docx import DocxDocumentExtractor

    filename, media_type = _docx_input(content)
    settings = config.docx
    return DocxDocumentExtractor(
        heading_numbers=settings.heading_numbers,
        include_images=settings.include_images,
        include_headers_footers=settings.include_headers_footers,
        vision_model=cast(VisionDescriptionPort | None, settings.vision_model),
        archive_limits=_archive_limits(config),
    ).extract(content.path, filename=filename, media_type=media_type)


@AsyncPipelineHooks.extractor("docx", "doc")
async def _extract_docx_async(
    content: LoaderContent, config: PipelineConfig
) -> ExtractedDocument:
    from comet_rag.engines.documents.docx import AsyncDocxDocumentExtractor

    filename, media_type = _docx_input(content)
    settings = config.docx
    return await AsyncDocxDocumentExtractor(
        heading_numbers=settings.heading_numbers,
        include_images=settings.include_images,
        include_headers_footers=settings.include_headers_footers,
        vision_model=cast(AsyncVisionDescriptionPort | None, settings.vision_model),
        archive_limits=_archive_limits(config),
    ).extract(content.path, filename=filename, media_type=media_type)


def _archive_limits(config: PipelineConfig) -> ArchiveLimits:
    settings = config.docx
    return ArchiveLimits(
        max_members=settings.max_archive_members,
        max_member_uncompressed_bytes=settings.max_archive_member_bytes,
        max_total_uncompressed_bytes=settings.max_archive_uncompressed_bytes,
        max_compression_ratio=settings.max_archive_compression_ratio,
        max_xml_elements=settings.max_archive_xml_elements,
        max_xml_text_chars=settings.max_archive_xml_text_chars,
    )


def _default_chunk(text: str, config: PipelineConfig) -> list[str]:
    from comet_rag.engines.chunkers import SEPARATORS_EN, RecursiveChunker

    return RecursiveChunker(
        config.chunk_size,
        config.chunk_overlap,
        separators=SEPARATORS_EN,
    ).chunk(text)


def _default_document_chunk(
    document: NormalizedDocument, config: PipelineConfig
) -> list[ChunkDraft]:
    from comet_rag.engines.chunkers import (
        SEPARATORS_EN,
        DocumentStructureError,
        MarkdownSectionStrategy,
        PageChunkingStrategy,
        RecursiveChunker,
    )

    chunker = RecursiveChunker(
        config.chunk_size,
        config.chunk_overlap,
        separators=SEPARATORS_EN,
    )
    if document.blocks:
        kinds = {block.kind for block in document.blocks}
        if kinds == {"page"}:
            return PageChunkingStrategy(chunker).split(document)
        if kinds == {"section"}:
            return MarkdownSectionStrategy(chunker).split(document)
        raise DocumentStructureError(
            f"没有适用于 DocumentBlock kinds={sorted(kinds)!r} 的内置分块策略"
        )
    return chunker.split(document.markdown)


PipelineHooks.document_chunker("docx", "doc")(_default_document_chunk)
AsyncPipelineHooks.document_chunker("docx", "doc")(_default_document_chunk)
