from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ClassVar

from comet_rag.engines.loaders.types import LoaderContent
from comet_rag.engines.pipelines.types import PipelineConfig

# Hook type aliases
ExtractHook = Callable[[LoaderContent, PipelineConfig], str]
ChunkHook = Callable[[str, PipelineConfig], list[str]]


@dataclass(frozen=True, slots=True)
class HooksState:
    """注册表的一份快照。由 `PipelineHooks.snapshot()` 产出，只应传回 `restore()`。"""

    extractors: dict[str, ExtractHook]
    chunkers: dict[str, ChunkHook]


class PipelineHooks:
    """
    Global registry for format-specific pipeline hooks.

    Two hook types:
      - extractor: LoaderContent, PipelineConfig → str  (convert + parse + clean)
      - chunker:   str, PipelineConfig → list[str]

    Register custom hooks to extend format support:

        @PipelineHooks.extractor("pdf")
        def extract_pdf(loader_content: LoaderContent, config: PipelineConfig) -> str:
            ...

        @PipelineHooks.chunker("pdf")
        def chunk_pdf(text: str, config: PipelineConfig) -> list[str]:
            ...

    注册表是**进程级全局**的，这让扩展格式只需 import 一个模块即可生效。
    代价是注册会互相泄漏：临时覆盖某个格式后，同进程内其余代码也会看到。
    需要限定作用域时用 `temporary()`：

        with PipelineHooks.temporary():
            @PipelineHooks.extractor("docx")
            def only_here(lc, config): ...
        # 出了 with 块，内置 docx extractor 自动恢复

    测试尤其依赖这一点 —— 没有它，A 用例注册的 hook 会跑进 B 用例，
    且失败与否取决于用例执行顺序。
    """

    _extractors: ClassVar[dict[str, ExtractHook]] = {}
    _chunkers: ClassVar[dict[str, ChunkHook]] = {}

    # ── 作用域控制 ─────────────────────────────────────────────────────────

    @classmethod
    def snapshot(cls) -> HooksState:
        """拍下当前注册表（浅拷贝：hook 函数本身不复制，也无需复制）。"""
        return HooksState(dict(cls._extractors), dict(cls._chunkers))

    @classmethod
    def restore(cls, state: HooksState) -> None:
        """还原到某次快照。快照之后新增的注册会被丢弃。"""
        cls._extractors = dict(state.extractors)
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
    def get_chunker(cls, file_type: str) -> ChunkHook:
        return cls._chunkers.get(file_type, _default_chunk)


# ── Built-in extractors ─────────────────────────────────────────────────────


@PipelineHooks.extractor("docx", "doc")
def _extract_docx(loader_content: LoaderContent, config: PipelineConfig) -> str:
    from comet_rag.engines.cleaners.docx_cleaner import DocxCleaner
    from comet_rag.engines.converters.text_converter import DocxConverter
    from comet_rag.engines.parsers.docx_parser.docx_parser import DocxParser

    doc = DocxConverter(loader_content).to_docx()
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
