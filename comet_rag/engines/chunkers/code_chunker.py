from __future__ import annotations

from collections.abc import Sequence

from comet_rag.engines.chunkers._length import LengthFunction
from comet_rag.engines.chunkers.profiles import (
    CodeLanguage,
    defaults_for_code,
    normalize_code_language,
    separators_for,
)
from comet_rag.engines.chunkers.recursive import RecursiveChunker, SeparatorPosition


class CodeRecursiveChunker(RecursiveChunker):
    """统一的代码递归分块器；语言只决定 separator profile 与默认预算。"""

    def __init__(
        self,
        code_language: CodeLanguage | str,
        *,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        separators: Sequence[str] | None = None,
        separator_position: SeparatorPosition = "start",
        length_function: LengthFunction = len,
    ) -> None:
        language = normalize_code_language(code_language)
        defaults = defaults_for_code(language)
        self.code_language = language
        super().__init__(
            chunk_size=defaults.chunk_size if chunk_size is None else chunk_size,
            chunk_overlap=(
                defaults.chunk_overlap if chunk_overlap is None else chunk_overlap
            ),
            separators=(
                separators if separators is not None else separators_for(language)
            ),
            separator_position=separator_position,
            length_function=length_function,
        )

__all__ = ["CodeRecursiveChunker"]
