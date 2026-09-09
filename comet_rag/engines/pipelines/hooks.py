from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import ClassVar

from comet_rag.engines.loaders.types import LoaderContent
from comet_rag.engines.pipelines.types import PipelineConfig

# Hook type aliases
ExtractHook = Callable[[LoaderContent, PipelineConfig], str]
AsyncExtractHook = Callable[[LoaderContent, PipelineConfig], Awaitable[str]]
ChunkHook = Callable[[str, PipelineConfig], list[str]]


@dataclass(frozen=True, slots=True)
class HooksState:
    """注册表的一份快照。由 `PipelineHooks.snapshot()` 产出，只应传回 `restore()`。"""

    extractors: dict[str, ExtractHook]
    chunkers: dict[str, ChunkHook]
    # 尾字段保留旧的 HooksState(extractors, chunkers) 构造方式。
    async_extractors: dict[str, AsyncExtractHook] = field(default_factory=dict)


class PipelineHooks:
    """进程级格式钩子注册表；临时覆盖必须用 `temporary()` 隔离。"""

    _extractors: ClassVar[dict[str, ExtractHook]] = {}
    _async_extractors: ClassVar[dict[str, AsyncExtractHook]] = {}
    _chunkers: ClassVar[dict[str, ChunkHook]] = {}

    # ── 作用域控制 ─────────────────────────────────────────────────────────

    @classmethod
    def snapshot(cls) -> HooksState:
        """拍下当前注册表（浅拷贝：hook 函数本身不复制，也无需复制）。"""
        return HooksState(
            extractors=dict(cls._extractors),
            chunkers=dict(cls._chunkers),
            async_extractors=dict(cls._async_extractors),
        )

    @classmethod
    def restore(cls, state: HooksState) -> None:
        """还原到某次快照。快照之后新增的注册会被丢弃。"""
        cls._extractors = dict(state.extractors)
        cls._async_extractors = dict(state.async_extractors)
        cls._chunkers = dict(state.chunkers)

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
    ) -> str:
        """优先使用异步 Hook；同步兼容路径只在线程池调度一次。"""
        if extractor := cls.get_aextractor(file_type):
            return await extractor(loader_content, config)
        return await asyncio.to_thread(
            cls.get_extractor(file_type), loader_content, config
        )

    @classmethod
    def get_chunker(cls, file_type: str) -> ChunkHook:
        return cls._chunkers.get(file_type, _default_chunk)


# ── Built-in extractors ─────────────────────────────────────────────────────


@PipelineHooks.extractor("docx", "doc")
def _extract_docx(loader_content: LoaderContent, config: PipelineConfig) -> str:
    from comet_rag.engines.cleaners.docx_cleaner import DocxCleaner
    from comet_rag.engines.converters.archive_guard import ArchiveLimits
    from comet_rag.engines.converters.text_converter import DocxConverter
    from comet_rag.engines.parsers.docx_parser.docx_parser import DocxParser

    doc = DocxConverter(
        loader_content,
        archive_limits=ArchiveLimits(
            max_members=config.docx.max_archive_members,
            max_member_uncompressed_bytes=config.docx.max_archive_member_bytes,
            max_total_uncompressed_bytes=config.docx.max_archive_uncompressed_bytes,
            max_compression_ratio=config.docx.max_archive_compression_ratio,
            max_xml_elements=config.docx.max_archive_xml_elements,
            max_xml_text_chars=config.docx.max_archive_xml_text_chars,
        ),
    ).to_docx()
    parsed = DocxParser(heading_numbers=config.docx.heading_numbers).parse(doc)
    return DocxCleaner(
        include_images=config.docx.include_images,
        include_headers_footers=config.docx.include_headers_footers,
        vision_model=config.docx.vision_model,
    ).clean_to_markdown(parsed)


# ── Built-in chunkers ───────────────────────────────────────────────────────


def _default_chunk(text: str, config: PipelineConfig) -> list[str]:
    from comet_rag.engines.chunkers.text_chunker import TextChunker

    return TextChunker(config.chunk_size, config.chunk_overlap).chunk(text)


@PipelineHooks.chunker("docx", "doc")
def _chunk_docx(text: str, config: PipelineConfig) -> list[str]:
    from comet_rag.engines.chunkers.text_chunker import DocxChunker

    return DocxChunker(config.chunk_size, config.chunk_overlap).chunk(text)
