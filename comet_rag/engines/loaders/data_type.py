from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, model_validator


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
    子类只需声明 `extensions` 和 `meta`，即自动注册到全局 Registry。
    """

    _registry: ClassVar[dict[str, type[BaseFileFormat]]] = {}

    extensions: ClassVar[tuple[str, ...]]
    meta: ClassVar[FormatMeta]

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
        return cls.meta.structure

    @classmethod
    def default_granularity(cls) -> GranularityStrategy:
        return cls.meta.default_granularity

    @classmethod
    def supports(cls, granularity: GranularityStrategy) -> bool:
        return cls.meta.supports(granularity)

    @classmethod
    def all_by_structure(
        cls, *structures: ContentStructure
    ) -> list[type[BaseFileFormat]]:
        seen: set[type[BaseFileFormat]] = set()
        result = []
        for fmt in cls._registry.values():
            if fmt not in seen and fmt.meta.structure in structures:
                seen.add(fmt)
                result.append(fmt)
        return result

    def __repr__(self) -> str:
        return f"<FileFormat {self.extensions}>"


_G = GranularityStrategy

_G_MAPS = {
    ContentStructure.PROSE: (_G.WHOLE, _G.BY_PAGE, _G.BY_CHUNK),
    ContentStructure.MIXED: (_G.WHOLE, _G.BY_PAGE, _G.BY_CHUNK),
    ContentStructure.SLIDE: (_G.WHOLE, _G.BY_PAGE, _G.BY_CHUNK),
    ContentStructure.TABULAR: (_G.BY_ROW,),
    ContentStructure.STRUCTURED: (_G.WHOLE, _G.BY_NODE),
    ContentStructure.CODE: (_G.WHOLE, _G.BY_CHUNK),
}


_FILE_EXTENSIONS = {
    ContentStructure.PROSE: ("txt",),
    ContentStructure.MIXED: ("doc", "docx", "pdf", "md"),
    ContentStructure.SLIDE: ("ppt", "pptx"),
    ContentStructure.TABULAR: ("csv", "xlsx", "xls"),
    ContentStructure.STRUCTURED: ("json", "yaml", "xml"),
    ContentStructure.CODE: ("py", "js", "ts", "java", "c", "cpp", "go"),
}


class ProseFormat(BaseFileFormat):
    extensions = _FILE_EXTENSIONS[ContentStructure.PROSE]
    meta = FormatMeta(ContentStructure.PROSE, _G.WHOLE, _G_MAPS[ContentStructure.PROSE])


class MixedFormat(BaseFileFormat):
    extensions = _FILE_EXTENSIONS[ContentStructure.MIXED]
    meta = FormatMeta(
        ContentStructure.MIXED, _G.BY_CHUNK, _G_MAPS[ContentStructure.MIXED]
    )


class SlideFormat(BaseFileFormat):
    extensions = _FILE_EXTENSIONS[ContentStructure.SLIDE]
    meta = FormatMeta(
        ContentStructure.SLIDE, _G.BY_PAGE, _G_MAPS[ContentStructure.SLIDE]
    )


class TabularFormat(BaseFileFormat):
    extensions = _FILE_EXTENSIONS[ContentStructure.TABULAR]
    meta = FormatMeta(
        ContentStructure.TABULAR, _G.BY_ROW, _G_MAPS[ContentStructure.TABULAR]
    )


class StructuredFormat(BaseFileFormat):
    extensions = _FILE_EXTENSIONS[ContentStructure.STRUCTURED]
    meta = FormatMeta(
        ContentStructure.STRUCTURED, _G.BY_NODE, _G_MAPS[ContentStructure.STRUCTURED]
    )


class CodeFormat(BaseFileFormat):
    extensions = _FILE_EXTENSIONS[ContentStructure.CODE]
    meta = FormatMeta(ContentStructure.CODE, _G.WHOLE, _G_MAPS[ContentStructure.CODE])


FileFormat = BaseFileFormat


class ParseConfig(BaseModel):
    model_config = ConfigDict(frozen=False)

    format_cls: type[BaseFileFormat]
    granularity: GranularityStrategy

    @model_validator(mode="after")
    def _validate_granularity(self) -> ParseConfig:
        if not self.format_cls.supports(self.granularity):
            allowed = ", ".join(
                g.value for g in self.format_cls.meta.supported_granularities
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
