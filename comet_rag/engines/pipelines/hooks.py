from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import ClassVar, Protocol

from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.engines.pipelines.types import PipelineConfig
from comet_rag.ports.document import (
    DocumentExtractorPort,
    ExtractedDocument,
    NormalizedDocument,
)
from comet_rag.ports.source import LoadedResource

LoaderContent = LoadedResource

# Hook type aliases
ExtractHook = Callable[[LoaderContent, PipelineConfig], ExtractedDocument]
AsyncExtractHook = Callable[
    [LoaderContent, PipelineConfig], Awaitable[ExtractedDocument]
]
ChunkHook = Callable[[str, PipelineConfig], list[str]]
DocumentChunkHook = Callable[
    [NormalizedDocument, PipelineConfig], list[ChunkDraft]
]


def adapt_legacy_chunk_hook(hook: ChunkHook) -> DocumentChunkHook:
    """把旧 ``str -> list[str]`` Hook 提升为文档级 Hook。

    旧返回值没有位置信息，不能用 ``str.find`` 猜测，故 span 明确留空。文档 metadata
    也不复制进 draft；统一合并由 Service 在 ChunkDraft 之后执行。
    """

    @wraps(hook)
    def adapted(
        document: NormalizedDocument, config: PipelineConfig
    ) -> list[ChunkDraft]:
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
class HooksState:
    """注册表的一份快照。由 `PipelineHooks.snapshot()` 产出，只应传回 `restore()`。"""

    extractors: dict[str, ExtractHook]
    chunkers: dict[str, ChunkHook]
    # 尾字段保留旧的 HooksState(extractors, chunkers) 构造方式。
    async_extractors: dict[str, AsyncExtractHook] = field(default_factory=dict)
    document_chunkers: dict[str, DocumentChunkHook] = field(default_factory=dict)


class HookProvider(Protocol):
    """Pipeline 实际需要的最小 Hook 查询面。"""

    def snapshot(self) -> HooksState: ...

    def restore(self, state: HooksState) -> None: ...

    def extractor(self, *file_types: str) -> Callable[[ExtractHook], ExtractHook]: ...

    def aextractor(
        self, *file_types: str
    ) -> Callable[[AsyncExtractHook], AsyncExtractHook]: ...

    def get_extractor(self, file_type: str) -> ExtractHook: ...

    def get_aextractor(self, file_type: str) -> AsyncExtractHook | None: ...

    async def aextract(
        self, file_type: str, loader_content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument: ...

    def get_chunker(self, file_type: str) -> ChunkHook: ...

    def document_chunker(
        self, *file_types: str
    ) -> Callable[[DocumentChunkHook], DocumentChunkHook]: ...

    def get_document_chunker(self, file_type: str) -> DocumentChunkHook: ...


class HookRegistry:
    """可按 Context 复制的 Hook 注册表，隔离带生命周期的适配器。"""

    def __init__(self, state: HooksState | None = None) -> None:
        state = state or HooksState({}, {})
        self._extractors = dict(state.extractors)
        self._async_extractors = dict(state.async_extractors)
        self._chunkers = dict(state.chunkers)
        self._document_chunkers = dict(state.document_chunkers)

    def snapshot(self) -> HooksState:
        return HooksState(
            extractors=dict(self._extractors),
            chunkers=dict(self._chunkers),
            async_extractors=dict(self._async_extractors),
            document_chunkers=dict(self._document_chunkers),
        )

    def restore(self, state: HooksState) -> None:
        self._extractors = dict(state.extractors)
        self._async_extractors = dict(state.async_extractors)
        self._chunkers = dict(state.chunkers)
        self._document_chunkers = dict(state.document_chunkers)

    @contextmanager
    def temporary(self) -> Iterator[None]:
        state = self.snapshot()
        try:
            yield
        finally:
            self.restore(state)

    def extractor(self, *file_types: str) -> Callable[[ExtractHook], ExtractHook]:
        def decorator(fn: ExtractHook) -> ExtractHook:
            for file_type in file_types:
                self._extractors[file_type.lower()] = fn
            return fn

        return decorator

    def aextractor(
        self, *file_types: str
    ) -> Callable[[AsyncExtractHook], AsyncExtractHook]:
        def decorator(fn: AsyncExtractHook) -> AsyncExtractHook:
            for file_type in file_types:
                self._async_extractors[file_type.lower()] = fn
            return fn

        return decorator

    def chunker(self, *file_types: str) -> Callable[[ChunkHook], ChunkHook]:
        def decorator(fn: ChunkHook) -> ChunkHook:
            for file_type in file_types:
                self._chunkers[file_type.lower()] = fn
            return fn

        return decorator

    def document_chunker(
        self, *file_types: str
    ) -> Callable[[DocumentChunkHook], DocumentChunkHook]:
        def decorator(fn: DocumentChunkHook) -> DocumentChunkHook:
            for file_type in file_types:
                self._document_chunkers[file_type.lower()] = fn
            return fn

        return decorator

    def get_extractor(self, file_type: str) -> ExtractHook:
        try:
            return self._extractors[file_type]
        except KeyError:
            raise ValueError(
                f"No extractor registered for {file_type!r}. "
                f"Registered: {sorted(self._extractors)}"
            ) from None

    def get_aextractor(self, file_type: str) -> AsyncExtractHook | None:
        return self._async_extractors.get(file_type)

    async def aextract(
        self, file_type: str, loader_content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        if extractor := self.get_aextractor(file_type):
            return await extractor(loader_content, config)
        return await asyncio.to_thread(
            self.get_extractor(file_type), loader_content, config
        )

    def get_chunker(self, file_type: str) -> ChunkHook:
        return self._chunkers.get(file_type, _default_chunk)

    def get_document_chunker(self, file_type: str) -> DocumentChunkHook:
        if chunker := self._document_chunkers.get(file_type):
            return chunker
        return adapt_legacy_chunk_hook(self.get_chunker(file_type))


class PipelineHooks:
    """进程级格式钩子注册表；临时覆盖必须用 `temporary()` 隔离。"""

    _extractors: ClassVar[dict[str, ExtractHook]] = {}
    _async_extractors: ClassVar[dict[str, AsyncExtractHook]] = {}
    _chunkers: ClassVar[dict[str, ChunkHook]] = {}
    _document_chunkers: ClassVar[dict[str, DocumentChunkHook]] = {}

    # ── 作用域控制 ─────────────────────────────────────────────────────────

    @classmethod
    def snapshot(cls) -> HooksState:
        """拍下当前注册表（浅拷贝：hook 函数本身不复制，也无需复制）。"""
        return HooksState(
            extractors=dict(cls._extractors),
            chunkers=dict(cls._chunkers),
            async_extractors=dict(cls._async_extractors),
            document_chunkers=dict(cls._document_chunkers),
        )

    @classmethod
    def fork(cls) -> HookRegistry:
        """复制内建/用户 Hook；之后的资源型注册只影响当前 Context。"""
        return HookRegistry(cls.snapshot())

    @classmethod
    def restore(cls, state: HooksState) -> None:
        """还原到某次快照。快照之后新增的注册会被丢弃。"""
        cls._extractors = dict(state.extractors)
        cls._async_extractors = dict(state.async_extractors)
        cls._chunkers = dict(state.chunkers)
        cls._document_chunkers = dict(state.document_chunkers)

    @classmethod
    @contextmanager
    def temporary(cls) -> Iterator[None]:
        """在块内注册/覆盖 hook，退出时自动还原（异常路径同样还原）。"""
        state = cls.snapshot()
        try:
            yield
        finally:
            cls.restore(state)

    @classmethod
    def extractor(cls, *file_types: str) -> Callable[[ExtractHook], ExtractHook]:
        def decorator(fn: ExtractHook) -> ExtractHook:
            for ft in file_types:
                cls._extractors[ft.lower()] = fn
            return fn

        return decorator

    @classmethod
    def aextractor(
        cls, *file_types: str
    ) -> Callable[[AsyncExtractHook], AsyncExtractHook]:
        def decorator(fn: AsyncExtractHook) -> AsyncExtractHook:
            for ft in file_types:
                cls._async_extractors[ft.lower()] = fn
            return fn

        return decorator

    @classmethod
    def chunker(cls, *file_types: str) -> Callable[[ChunkHook], ChunkHook]:
        def decorator(fn: ChunkHook) -> ChunkHook:
            for ft in file_types:
                cls._chunkers[ft.lower()] = fn
            return fn

        return decorator

    @classmethod
    def document_chunker(
        cls, *file_types: str
    ) -> Callable[[DocumentChunkHook], DocumentChunkHook]:
        def decorator(fn: DocumentChunkHook) -> DocumentChunkHook:
            for file_type in file_types:
                cls._document_chunkers[file_type.lower()] = fn
            return fn

        return decorator

    @classmethod
    def get_extractor(cls, file_type: str) -> ExtractHook:
        try:
            return cls._extractors[file_type]
        except KeyError:
            raise ValueError(
                f"No extractor registered for {file_type!r}. "
                f"Registered: {sorted(cls._extractors)}"
            ) from None

    @classmethod
    def get_aextractor(cls, file_type: str) -> AsyncExtractHook | None:
        return cls._async_extractors.get(file_type)

    @classmethod
    async def aextract(
        cls, file_type: str, loader_content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        """优先使用异步 Hook；同步兼容路径只在线程池调度一次。"""
        if extractor := cls.get_aextractor(file_type):
            return await extractor(loader_content, config)
        return await asyncio.to_thread(
            cls.get_extractor(file_type), loader_content, config
        )

    @classmethod
    def get_chunker(cls, file_type: str) -> ChunkHook:
        return cls._chunkers.get(file_type, _default_chunk)

    @classmethod
    def get_document_chunker(cls, file_type: str) -> DocumentChunkHook:
        if chunker := cls._document_chunkers.get(file_type):
            return chunker
        return adapt_legacy_chunk_hook(cls.get_chunker(file_type))


# ── Built-in extractors ─────────────────────────────────────────────────────


def _docx_extractor(config: PipelineConfig) -> DocumentExtractorPort:
    from comet_rag.engines.documents.common.archive import ArchiveLimits
    from comet_rag.engines.documents.docx import DocxDocumentExtractor

    return DocxDocumentExtractor(
        heading_numbers=config.docx.heading_numbers,
        include_images=config.docx.include_images,
        include_headers_footers=config.docx.include_headers_footers,
        vision_model=config.docx.vision_model,
        archive_limits=ArchiveLimits(
            max_members=config.docx.max_archive_members,
            max_member_uncompressed_bytes=config.docx.max_archive_member_bytes,
            max_total_uncompressed_bytes=config.docx.max_archive_uncompressed_bytes,
            max_compression_ratio=config.docx.max_archive_compression_ratio,
            max_xml_elements=config.docx.max_archive_xml_elements,
            max_xml_text_chars=config.docx.max_archive_xml_text_chars,
        ),
    )


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
def _extract_docx(
    loader_content: LoaderContent, config: PipelineConfig
) -> ExtractedDocument:
    filename, media_type = _docx_input(loader_content)
    return _docx_extractor(config).extract(
        loader_content.path,
        filename=filename,
        media_type=media_type,
    )


@PipelineHooks.aextractor("docx", "doc")
async def _aextract_docx(
    loader_content: LoaderContent, config: PipelineConfig
) -> ExtractedDocument:
    filename, media_type = _docx_input(loader_content)
    return await _docx_extractor(config).aextract(
        loader_content.path,
        filename=filename,
        media_type=media_type,
    )


# ── Built-in chunkers ───────────────────────────────────────────────────────


def _default_chunk(text: str, config: PipelineConfig) -> list[str]:
    from comet_rag.engines.chunkers.text_chunker import TextChunker

    return TextChunker(config.chunk_size, config.chunk_overlap).chunk(text)


@PipelineHooks.chunker("docx", "doc")
def _chunk_docx(text: str, config: PipelineConfig) -> list[str]:
    from comet_rag.engines.chunkers.text_chunker import DocxChunker

    return DocxChunker(config.chunk_size, config.chunk_overlap).chunk(text)
