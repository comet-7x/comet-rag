from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

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


class _LegacyCodeChunker(CodeRecursiveChunker):
    """旧语言类的薄门面；算法只由 CodeRecursiveChunker 实现。"""

    _code_language: ClassVar[CodeLanguage]

    def __init__(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        separators: list[str] | None = None,
        keep_separator: bool = True,
        keep_separator_at_start: bool = True,
    ) -> None:
        # 新算法始终保留原文分隔符，才能让 span 精确引用规范 Markdown。
        # keep_separator 只为兼容旧构造签名；False 不再删除后重插分隔符。
        _ = keep_separator
        super().__init__(
            self._code_language,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=separators,
            separator_position="start" if keep_separator_at_start else "end",
        )


class PythonChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.PY


class TypeScriptChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.TS


class JavaScriptChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.JS


class JavaChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.JAVA


class CChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.C


class CppChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.CPP


class GoChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.GO


class PhpChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.PHP


class RChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.R


class RustChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.RUST


class HtmlChunker(_LegacyCodeChunker):
    _code_language = CodeLanguage.HTML


__all__ = [
    "CChunker",
    "CodeRecursiveChunker",
    "CppChunker",
    "GoChunker",
    "HtmlChunker",
    "JavaChunker",
    "JavaScriptChunker",
    "PhpChunker",
    "PythonChunker",
    "RChunker",
    "RustChunker",
    "TypeScriptChunker",
]
