from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

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
    SEPARATORS_CSV,
    SEPARATORS_EN,
    SEPARATORS_JA,
    SEPARATORS_JSON,
    SEPARATORS_KO,
    SEPARATORS_MDX,
    SEPARATORS_XML,
    SEPARATORS_ZH,
)

SeparatorPosition = Literal["start", "end"]


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
class ChunkProfile:
    """递归算法的不可变参数画像；画像不是新的分块器类型。"""

    chunk_size: int
    chunk_overlap: int
    separators: tuple[str, ...]
    separator_position: SeparatorPosition = "end"


TEXT_PROFILE = ChunkProfile(1500, 150, SEPARATORS_EN)
DOCX_PROFILE = ChunkProfile(2500, 250, SEPARATORS_EN)
MARKDOWN_PROFILE = ChunkProfile(
    3000,
    300,
    SEPARATORS_MDX,
    separator_position="start",
)
CSV_PROFILE = ChunkProfile(
    1200,
    100,
    SEPARATORS_CSV,
    separator_position="start",
)
JSON_PROFILE = ChunkProfile(2000, 200, SEPARATORS_JSON)
XML_PROFILE = ChunkProfile(2500, 250, SEPARATORS_XML)


_LANGUAGE_SEPARATORS: dict[Language | CodeLanguage, tuple[str, ...]] = {
    Language.ENGLISH: SEPARATORS_EN,
    Language.CHINESE: SEPARATORS_ZH,
    Language.JAPANESE: SEPARATORS_JA,
    Language.KOREAN: SEPARATORS_KO,
    CodeLanguage.PY: SEPARATORS_CODE_PY,
    CodeLanguage.TS: SEPARATORS_CODE_TS,
    CodeLanguage.JS: SEPARATORS_CODE_JS,
    CodeLanguage.JAVA: SEPARATORS_CODE_JAVA,
    CodeLanguage.C: SEPARATORS_CODE_C,
    CodeLanguage.CPP: SEPARATORS_CODE_CPP,
    CodeLanguage.GO: SEPARATORS_CODE_GO,
    CodeLanguage.PHP: SEPARATORS_CODE_PHP,
    CodeLanguage.R: SEPARATORS_CODE_R,
    CodeLanguage.RUST: SEPARATORS_CODE_RUST,
    CodeLanguage.HTML: SEPARATORS_CODE_HTML,
}

_CODE_PROFILES = {
    CodeLanguage.PY: ChunkProfile(
        1500, 150, _LANGUAGE_SEPARATORS[CodeLanguage.PY], "start"
    ),
    CodeLanguage.TS: ChunkProfile(
        1500, 150, _LANGUAGE_SEPARATORS[CodeLanguage.TS], "start"
    ),
    CodeLanguage.JS: ChunkProfile(
        1200, 100, _LANGUAGE_SEPARATORS[CodeLanguage.JS], "start"
    ),
    CodeLanguage.JAVA: ChunkProfile(
        2000, 200, _LANGUAGE_SEPARATORS[CodeLanguage.JAVA], "start"
    ),
    CodeLanguage.C: ChunkProfile(
        1000, 100, _LANGUAGE_SEPARATORS[CodeLanguage.C], "start"
    ),
    CodeLanguage.CPP: ChunkProfile(
        1500, 150, _LANGUAGE_SEPARATORS[CodeLanguage.CPP], "start"
    ),
    CodeLanguage.GO: ChunkProfile(
        1000, 100, _LANGUAGE_SEPARATORS[CodeLanguage.GO], "start"
    ),
    CodeLanguage.PHP: ChunkProfile(
        1200, 100, _LANGUAGE_SEPARATORS[CodeLanguage.PHP], "start"
    ),
    CodeLanguage.R: ChunkProfile(
        1000, 100, _LANGUAGE_SEPARATORS[CodeLanguage.R], "start"
    ),
    CodeLanguage.RUST: ChunkProfile(
        1500, 150, _LANGUAGE_SEPARATORS[CodeLanguage.RUST], "start"
    ),
    CodeLanguage.HTML: ChunkProfile(
        1500, 200, _LANGUAGE_SEPARATORS[CodeLanguage.HTML], "start"
    ),
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


def language_profile(
    language: Language = Language.ENGLISH,
    *,
    chunk_size: int = TEXT_PROFILE.chunk_size,
    chunk_overlap: int = TEXT_PROFILE.chunk_overlap,
) -> ChunkProfile:
    return ChunkProfile(
        chunk_size,
        chunk_overlap,
        separators_for(language),
    )


def code_profile(
    code_language: CodeLanguage | str,
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> ChunkProfile:
    language = normalize_code_language(code_language)
    profile = _CODE_PROFILES[language]
    return ChunkProfile(
        profile.chunk_size if chunk_size is None else chunk_size,
        profile.chunk_overlap if chunk_overlap is None else chunk_overlap,
        profile.separators,
        profile.separator_position,
    )


__all__ = [
    "CSV_PROFILE",
    "DOCX_PROFILE",
    "JSON_PROFILE",
    "MARKDOWN_PROFILE",
    "TEXT_PROFILE",
    "XML_PROFILE",
    "ChunkProfile",
    "CodeLanguage",
    "Language",
    "SeparatorPosition",
    "code_profile",
    "language_profile",
    "normalize_code_language",
    "separators_for",
]
