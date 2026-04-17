from comet_rag.engines.chunkers.base_chunker import BaseChunker, CodeLanguage


class PythonChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.PY,
            keep_separator_at_start,
        )


class TypeScriptChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.TS,
            keep_separator_at_start,
        )


class JavaScriptChunker(BaseChunker):
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
            separators,
            keep_separator,
            CodeLanguage.JS,
            keep_separator_at_start,
        )


class JavaChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 2000,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.JAVA,
            keep_separator_at_start,
        )


class CChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.C,
            keep_separator_at_start,
        )


class CppChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.CPP,
            keep_separator_at_start,
        )


class GoChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.GO,
            keep_separator_at_start,
        )


class PhpChunker(BaseChunker):
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
            separators,
            keep_separator,
            CodeLanguage.PHP,
            keep_separator_at_start,
        )


class RChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.R,
            keep_separator_at_start,
        )


class RustChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.RUST,
            keep_separator_at_start,
        )


class HtmlChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ):
        super().__init__(
            chunk_size,
            chunk_overlap,
            separators,
            keep_separator,
            CodeLanguage.HTML,
            keep_separator_at_start,
        )
