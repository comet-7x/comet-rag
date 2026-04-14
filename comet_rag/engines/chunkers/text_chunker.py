from comet_rag.engines.chunkers.base_chunker import BaseChunker, Language


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
        if separators is None:
            separators = [
                "\n# ",  # H1 headers (top-level sections)
                "\n## ",  # H2 headers (major sections)
                "\n### ",  # H3 headers (subsections)
                "\n#### ",  # H4 headers (sub-subsections)
                "\n\n",  # Paragraph breaks
                "\n```",  # Code block boundaries
                "\n",  # Line breaks
                ". ",  # Sentence endings
                "! ",  # Exclamation endings
                "? ",  # Question endings
                "; ",  # Semicolon breaks
                ", ",  # Comma breaks
                " ",  # Word breaks
                "",  # Character level
            ]
        super().__init__(chunk_size, chunk_overlap, separators, keep_separator)

    def chunk(self, text: str) -> list[str]:
        # \n 前缀的分隔符无法匹配文档首行标题，预置 \n 使其可被正常识别
        if text and text[0] == "#":
            text = "\n" + text
        return super().chunk(text)
