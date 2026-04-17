from comet_rag.engines.chunkers.base_chunker import BaseChunker, CodeLanguage


class PythonChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.PY
        )


class TypeScriptChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.TS
        )


class JavaScriptChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1200,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.JS
        )


class JavaCodeChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 2000,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.JAVA
        )


class CChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.C
        )


class CppChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.CPP
        )


class GoChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.GO
        )


class PhpChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1200,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.PHP
        )


class RChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.R
        )


class RustChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 150,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.RUST
        )


class HtmlChunker(BaseChunker):
    def __init__(
        self,
        chunk_size: int = 1500,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
        keep_separator: bool = True,
    ):
        super().__init__(
            chunk_size, chunk_overlap, separators, keep_separator, CodeLanguage.HTML
        )
