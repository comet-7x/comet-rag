from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, model_validator


class AllowExt(StrEnum):
    TXT = "txt"
    DOC = "doc"
    DOCX = "docx"
    PDF = "pdf"
    MD = "md"
    PPT = "ppt"
    PPTX = "pptx"

    CSV = "csv"
    XLSX = "xlsx"
    XLS = "xls"

    JSON = "json"
    YAML = "yaml"
    XML = "xml"

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


def normalize_extension(extension: str) -> str:
    """Return the canonical extension form used by the format registry."""

    return extension.strip().lower().lstrip(".")


def is_allowed_extension(extension: str) -> bool:
    """Check extension support without relying on Enum implementation details."""

    try:
        AllowExt(normalize_extension(extension))
    except ValueError:
        return False
    return True


class ContentStructure(StrEnum):
    PROSE = "prose"
    TABULAR = "tabular"
    MIXED = "mixed"
    SLIDE = "slide"
    STRUCTURED = "structured"
    CODE = "code"


class GranularityStrategy(StrEnum):
    WHOLE = "whole"
    BY_PAGE = "by_page"
    BY_CHUNK = "by_chunk"
    BY_ROW = "by_row"
    BY_NODE = "by_node"


@dataclass(frozen=True)
class FormatMeta:
    structure: ContentStructure
    default_granularity: GranularityStrategy
    supported_granularities: tuple[GranularityStrategy, ...]

    def supports(self, granularity: GranularityStrategy) -> bool:
        return granularity in self.supported_granularities


class BaseFileFormat:
    """A declarative file-format family.

    Registration is explicit in ``_FORMAT_TYPES`` below. This avoids import-time
    mutation and makes the complete format table visible in one place.
    """

    extensions: ClassVar[tuple[AllowExt, ...]]
    format_meta: ClassVar[FormatMeta]

    @classmethod
    def from_extension(cls, ext: str) -> type[BaseFileFormat]:
        normalized = normalize_extension(ext)
        try:
            return _FORMAT_BY_EXTENSION[normalized]
        except KeyError as e:
            raise ValueError(
                f"未知扩展名：{normalized!r}，已注册：{sorted(_FORMAT_BY_EXTENSION)}"
            ) from e

    @classmethod
    def from_path(cls, path: str | Path) -> type[BaseFileFormat]:
        path_str = str(path)
        if "://" in path_str:
            from urllib.parse import urlparse

            path_str = urlparse(path_str).path
        return cls.from_extension(Path(path_str).suffix)

    @classmethod
    def structure(cls) -> ContentStructure:
        return cls.format_meta.structure

    @classmethod
    def default_granularity(cls) -> GranularityStrategy:
        return cls.format_meta.default_granularity

    @classmethod
    def supports(cls, granularity: GranularityStrategy) -> bool:
        return cls.format_meta.supports(granularity)

    @classmethod
    def all_by_structure(
        cls, *structures: ContentStructure
    ) -> list[type[BaseFileFormat]]:
        return [
            fmt for fmt in _FORMAT_TYPES if fmt.format_meta.structure in structures
        ]

    def __repr__(self) -> str:
        return f"<FileFormat {self.extensions}>"


class ProseFormat(BaseFileFormat):
    extensions = (AllowExt.TXT,)
    format_meta = FormatMeta(
        structure=ContentStructure.PROSE,
        default_granularity=GranularityStrategy.WHOLE,
        supported_granularities=(
            GranularityStrategy.WHOLE,
            GranularityStrategy.BY_PAGE,
            GranularityStrategy.BY_CHUNK,
        ),
    )


class MixedFormat(BaseFileFormat):
    extensions = (AllowExt.DOC, AllowExt.DOCX, AllowExt.PDF, AllowExt.MD)
    format_meta = FormatMeta(
        structure=ContentStructure.MIXED,
        default_granularity=GranularityStrategy.BY_CHUNK,
        supported_granularities=(
            GranularityStrategy.WHOLE,
            GranularityStrategy.BY_PAGE,
            GranularityStrategy.BY_CHUNK,
        ),
    )


class SlideFormat(BaseFileFormat):
    extensions = (AllowExt.PPT, AllowExt.PPTX)
    format_meta = FormatMeta(
        structure=ContentStructure.SLIDE,
        default_granularity=GranularityStrategy.BY_PAGE,
        supported_granularities=(
            GranularityStrategy.WHOLE,
            GranularityStrategy.BY_PAGE,
            GranularityStrategy.BY_CHUNK,
        ),
    )


class TabularFormat(BaseFileFormat):
    extensions = (AllowExt.CSV, AllowExt.XLSX, AllowExt.XLS)
    format_meta = FormatMeta(
        structure=ContentStructure.TABULAR,
        default_granularity=GranularityStrategy.BY_ROW,
        supported_granularities=(GranularityStrategy.BY_ROW,),
    )


class StructuredFormat(BaseFileFormat):
    extensions = (AllowExt.JSON, AllowExt.YAML, AllowExt.XML)
    format_meta = FormatMeta(
        structure=ContentStructure.STRUCTURED,
        default_granularity=GranularityStrategy.BY_NODE,
        supported_granularities=(
            GranularityStrategy.WHOLE,
            GranularityStrategy.BY_NODE,
        ),
    )


class CodeFormat(BaseFileFormat):
    extensions = (
        AllowExt.PY,
        AllowExt.TS,
        AllowExt.JS,
        AllowExt.JAVA,
        AllowExt.C,
        AllowExt.CPP,
        AllowExt.GO,
        AllowExt.PHP,
        AllowExt.R,
        AllowExt.RUST,
        AllowExt.HTML,
    )
    format_meta = FormatMeta(
        structure=ContentStructure.CODE,
        default_granularity=GranularityStrategy.WHOLE,
        supported_granularities=(
            GranularityStrategy.WHOLE,
            GranularityStrategy.BY_CHUNK,
        ),
    )


_FORMAT_TYPES: tuple[type[BaseFileFormat], ...] = (
    ProseFormat,
    MixedFormat,
    SlideFormat,
    TabularFormat,
    StructuredFormat,
    CodeFormat,
)


def _build_format_registry() -> dict[str, type[BaseFileFormat]]:
    registry: dict[str, type[BaseFileFormat]] = {}
    for format_type in _FORMAT_TYPES:
        for extension in format_type.extensions:
            key = extension.value
            if key in registry:
                raise RuntimeError(f"Duplicate file format extension: {key!r}")
            registry[key] = format_type
    return registry


_FORMAT_BY_EXTENSION = _build_format_registry()


FileFormat = BaseFileFormat


class ParseConfig(BaseModel):
    model_config = ConfigDict(frozen=False)

    format_cls: type[BaseFileFormat]
    granularity: GranularityStrategy

    @model_validator(mode="after")
    def _validate_granularity(self) -> ParseConfig:
        if not self.format_cls.supports(self.granularity):
            allowed = ", ".join(
                g.value for g in self.format_cls.format_meta.supported_granularities
            )
            raise ValueError(
                f"{self.format_cls.extensions} 不支持 {self.granularity}，可选：{allowed}"
            )
        return self

    @classmethod
    def from_format(cls, fmt: type[BaseFileFormat]) -> ParseConfig:
        return cls(format_cls=fmt, granularity=fmt.default_granularity())

    @classmethod
    def from_path(cls, path: str | Path) -> ParseConfig:
        return cls.from_format(BaseFileFormat.from_path(path))

    @classmethod
    def from_extension(cls, ext: str) -> ParseConfig:
        return cls.from_format(BaseFileFormat.from_extension(ext))
