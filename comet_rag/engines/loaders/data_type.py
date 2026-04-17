from __future__ import annotations

from abc import ABC
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


class BaseFileFormat(ABC):
    """
    所有文件格式的抽象基类。
    子类只需声明 `extensions` 和 `format_meta`，即自动注册到全局 Registry。
    """

    _registry: ClassVar[dict[str, type[BaseFileFormat]]] = {}

    extensions: ClassVar[tuple[str, ...]]
    format_meta: ClassVar[FormatMeta]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if ABC in cls.__bases__:
            return
        for ext in cls.extensions:
            BaseFileFormat._registry[ext] = cls

    @classmethod
    def from_extension(cls, ext: str) -> type[BaseFileFormat]:
        ext = ext.lower().lstrip(".")
        try:
            return cls._registry[ext]
        except KeyError as e:
            raise ValueError(
                f"未知扩展名：{ext!r}，已注册：{sorted(cls._registry)}"
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
        seen: set[type[BaseFileFormat]] = set()
        formats = []
        for fmt in cls._registry.values():
            if fmt not in seen and fmt.format_meta.structure in structures:
                seen.add(fmt)
                formats.append(fmt)
        return formats

    def __repr__(self) -> str:
        return f"<FileFormat {self.extensions}>"


_GS = GranularityStrategy

_GS_MAPS = {
    ContentStructure.PROSE: (_GS.WHOLE, _GS.BY_PAGE, _GS.BY_CHUNK),
    ContentStructure.MIXED: (_GS.WHOLE, _GS.BY_PAGE, _GS.BY_CHUNK),
    ContentStructure.SLIDE: (_GS.WHOLE, _GS.BY_PAGE, _GS.BY_CHUNK),
    ContentStructure.TABULAR: (_GS.BY_ROW,),
    ContentStructure.STRUCTURED: (_GS.WHOLE, _GS.BY_NODE),
    ContentStructure.CODE: (_GS.WHOLE, _GS.BY_CHUNK),
}


_EXTENSIONS_BY_STRUCTURE = {
    ContentStructure.PROSE: (AllowExt.TXT,),
    ContentStructure.MIXED: (AllowExt.DOC, AllowExt.DOCX, AllowExt.PDF, AllowExt.MD),
    ContentStructure.SLIDE: (AllowExt.PPT, AllowExt.PPTX),
    ContentStructure.TABULAR: (AllowExt.CSV, AllowExt.XLSX, AllowExt.XLS),
    ContentStructure.STRUCTURED: (AllowExt.JSON, AllowExt.YAML, AllowExt.XML),
    ContentStructure.CODE: (
        AllowExt.PY,
        AllowExt.TS,
        AllowExt.JS,
        AllowExt.JAVA,
        AllowExt.C,
        AllowExt.CPP,
        AllowExt.GO,
    ),
}


class ProseFormat(BaseFileFormat):
    extensions = _EXTENSIONS_BY_STRUCTURE[ContentStructure.PROSE]
    format_meta = FormatMeta(
        ContentStructure.PROSE, _GS.WHOLE, _GS_MAPS[ContentStructure.PROSE]
    )


class MixedFormat(BaseFileFormat):
    extensions = _EXTENSIONS_BY_STRUCTURE[ContentStructure.MIXED]
    format_meta = FormatMeta(
        ContentStructure.MIXED, _GS.BY_CHUNK, _GS_MAPS[ContentStructure.MIXED]
    )


class SlideFormat(BaseFileFormat):
    extensions = _EXTENSIONS_BY_STRUCTURE[ContentStructure.SLIDE]
    format_meta = FormatMeta(
        ContentStructure.SLIDE, _GS.BY_PAGE, _GS_MAPS[ContentStructure.SLIDE]
    )


class TabularFormat(BaseFileFormat):
    extensions = _EXTENSIONS_BY_STRUCTURE[ContentStructure.TABULAR]
    format_meta = FormatMeta(
        ContentStructure.TABULAR, _GS.BY_ROW, _GS_MAPS[ContentStructure.TABULAR]
    )


class StructuredFormat(BaseFileFormat):
    extensions = _EXTENSIONS_BY_STRUCTURE[ContentStructure.STRUCTURED]
    format_meta = FormatMeta(
        ContentStructure.STRUCTURED, _GS.BY_NODE, _GS_MAPS[ContentStructure.STRUCTURED]
    )


class CodeFormat(BaseFileFormat):
    extensions = _EXTENSIONS_BY_STRUCTURE[ContentStructure.CODE]
    format_meta = FormatMeta(
        ContentStructure.CODE, _GS.WHOLE, _GS_MAPS[ContentStructure.CODE]
    )


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
