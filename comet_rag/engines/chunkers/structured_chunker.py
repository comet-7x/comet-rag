from comet_rag.engines.chunkers.base_chunker import BaseChunker
from comet_rag.engines.chunkers.separators import (
    SEPARATORS_CSV,
    SEPARATORS_JSON,
    SEPARATORS_XML,
)


class CsvChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1200,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators if separators is not None else SEPARATORS_CSV,
            keep_separator,
            keep_separator_at_start=keep_separator_at_start,
        )


class JsonChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 2000,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators if separators is not None else SEPARATORS_JSON,
            keep_separator,
        )


class XmlChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 2500,
        chunk_overlap: int = 250,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators if separators is not None else SEPARATORS_XML,
            keep_separator,
        )
