from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from comet_rag.engines.chunkers.separators import (
    SEPARATORS_CODE_C,
    SEPARATORS_CODE_CPP,
    SEPARATORS_CODE_GO,
    SEPARATORS_CODE_HTML,
    SEPARATORS_CODE_JAVA,
    SEPARATORS_CODE_JS,
    SEPARATORS_CODE_PHP,
    SEPARATORS_CODE_PY,
    SEPARATORS_CODE_R,
    SEPARATORS_CODE_RUST,
    SEPARATORS_CODE_TS,
    SEPARATORS_EN,
    SEPARATORS_JA,
    SEPARATORS_KO,
    SEPARATORS_ZH,
)


class Language(StrEnum):
    ENGLISH = "en"
    CHINESE = "zh"
    JAPANESE = "ja"
    KOREAN = "ko"


class CodeLanguage(StrEnum):
    PY = "py"
    TS = "ts"
    JS = "js"
    JAVA = "java"
    C = "c"
    CPP = "cpp"
    GO = "go"
    PHP = "php"
    R = "r"
    RUST = "rust"
    HTML = "html"


@dataclass(frozen=True, slots=True)
class ChunkingDefaults:
    chunk_size: int
    chunk_overlap: int


_LANGUAGE_SEPARATORS: dict[Language | CodeLanguage, tuple[str, ...]] = {
    Language.ENGLISH: tuple(SEPARATORS_EN),
    Language.CHINESE: tuple(SEPARATORS_ZH),
    Language.JAPANESE: tuple(SEPARATORS_JA),
    Language.KOREAN: tuple(SEPARATORS_KO),
    CodeLanguage.PY: tuple(SEPARATORS_CODE_PY),
    CodeLanguage.TS: tuple(SEPARATORS_CODE_TS),
    CodeLanguage.JS: tuple(SEPARATORS_CODE_JS),
    CodeLanguage.JAVA: tuple(SEPARATORS_CODE_JAVA),
    CodeLanguage.C: tuple(SEPARATORS_CODE_C),
    CodeLanguage.CPP: tuple(SEPARATORS_CODE_CPP),
    CodeLanguage.GO: tuple(SEPARATORS_CODE_GO),
    CodeLanguage.PHP: tuple(SEPARATORS_CODE_PHP),
    CodeLanguage.R: tuple(SEPARATORS_CODE_R),
    CodeLanguage.RUST: tuple(SEPARATORS_CODE_RUST),
    CodeLanguage.HTML: tuple(SEPARATORS_CODE_HTML),
}

_CODE_DEFAULTS = {
    CodeLanguage.PY: ChunkingDefaults(1500, 150),
    CodeLanguage.TS: ChunkingDefaults(1500, 150),
    CodeLanguage.JS: ChunkingDefaults(1200, 100),
    CodeLanguage.JAVA: ChunkingDefaults(2000, 200),
    CodeLanguage.C: ChunkingDefaults(1000, 100),
    CodeLanguage.CPP: ChunkingDefaults(1500, 150),
    CodeLanguage.GO: ChunkingDefaults(1000, 100),
    CodeLanguage.PHP: ChunkingDefaults(1200, 100),
    CodeLanguage.R: ChunkingDefaults(1000, 100),
    CodeLanguage.RUST: ChunkingDefaults(1500, 150),
    CodeLanguage.HTML: ChunkingDefaults(1500, 200),
}

_CODE_ALIASES = {
    "c++": CodeLanguage.CPP,
    "javascript": CodeLanguage.JS,
    "python": CodeLanguage.PY,
    "rs": CodeLanguage.RUST,
    "typescript": CodeLanguage.TS,
}


def normalize_code_language(value: CodeLanguage | str) -> CodeLanguage:
    if isinstance(value, CodeLanguage):
        return value
    normalized = value.lower().strip().removeprefix(".")
    if alias := _CODE_ALIASES.get(normalized):
        return alias
    try:
        return CodeLanguage(normalized)
    except ValueError:
        supported = ", ".join(language.value for language in CodeLanguage)
        raise ValueError(
            f"不支持的 code_language {value!r}；可选值：{supported}"
        ) from None


def separators_for(language: Language | CodeLanguage) -> tuple[str, ...]:
    return _LANGUAGE_SEPARATORS[language]


def defaults_for_code(language: CodeLanguage) -> ChunkingDefaults:
    return _CODE_DEFAULTS[language]


__all__ = [
    "ChunkingDefaults",
    "CodeLanguage",
    "Language",
    "defaults_for_code",
    "normalize_code_language",
    "separators_for",
]
