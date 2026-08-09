from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from comet_rag.engines.loaders.types import LoaderContent
from comet_rag.engines.pipelines.types import PipelineConfig

# Hook type aliases
ExtractHook = Callable[[LoaderContent, PipelineConfig], str]
ChunkHook = Callable[[str, PipelineConfig], list[str]]


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
    """

    _extractors: ClassVar[dict[str, ExtractHook]] = {}
    _chunkers: ClassVar[dict[str, ChunkHook]] = {}

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
