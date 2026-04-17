from comet_rag.engines.chunkers.base_chunker import BaseChunker, Language
from comet_rag.engines.chunkers.separators import SEPARATORS_MDX


class TextChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        language: Language = Language.ENGLISH,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, language
        )


class DocxChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 2500,
        chunk_overlap: int = 250,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        language: Language = Language.ENGLISH,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, language
        )


class MdxChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 3000,
        chunk_overlap: int = 300,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators if separators is not None else SEPARATORS_MDX,
            keep_separator,
        )

    def chunk(self, text: str) -> list[str]:
        # \n 前缀的分隔符无法匹配文档首行标题，预置 \n 使其可被正常识别
        if text and text[0] == "#":
            text = "\n" + text
        return super().chunk(text)
