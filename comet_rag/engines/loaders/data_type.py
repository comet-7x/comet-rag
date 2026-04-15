from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, model_validator


class FormatCategory(StrEnum):
    DOC = "doc"
    SHEET = "sheet"
    CODE = "code"


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
    category: FormatCategory
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
        if ABC in cls.__bases__:  # 跳过抽象中间层
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
        return cls.from_extension(Path(path).suffix)

    @classmethod
    def structure(cls) -> ContentStructure:
        return cls.meta.structure

    @classmethod
    def category(cls) -> FormatCategory:
        return cls.meta.category

    @classmethod
    def default_granularity(cls) -> GranularityStrategy:
        return cls.meta.default_granularity

    @classmethod
    def supports(cls, granularity: GranularityStrategy) -> bool:
        return cls.meta.supports(granularity)

    @classmethod
    def all_by_category(cls, category: FormatCategory) -> list[type[BaseFileFormat]]:
        seen: set[type[BaseFileFormat]] = set()
        result = []
        for fmt in cls._registry.values():
            if fmt not in seen and fmt.meta.category == category:
                seen.add(fmt)
                result.append(fmt)
        return result

    def __repr__(self) -> str:
        return f"<FileFormat {self.extensions}>"


_G = GranularityStrategy
_DOC_GRANULARITIES = (_G.WHOLE, _G.BY_PAGE, _G.BY_CHUNK)
_SLIDE_GRANULARITIES = (_G.WHOLE, _G.BY_PAGE, _G.BY_CHUNK)
_CODE_GRANULARITIES = (_G.WHOLE, _G.BY_CHUNK)
_NODE_GRANULARITIES = (_G.WHOLE, _G.BY_NODE)


class ProseFormat(BaseFileFormat):
    extensions = ("txt",)
    meta = FormatMeta(
        FormatCategory.DOC, ContentStructure.PROSE, _G.WHOLE, _DOC_GRANULARITIES
    )


class MixedFormat(BaseFileFormat):
    extensions = ("doc", "docx", "pdf", "md")
    meta = FormatMeta(
        FormatCategory.DOC, ContentStructure.MIXED, _G.BY_CHUNK, _DOC_GRANULARITIES
    )


class SlideFormat(BaseFileFormat):
    extensions = ("ppt", "pptx")
    meta = FormatMeta(
        FormatCategory.DOC, ContentStructure.SLIDE, _G.BY_PAGE, _SLIDE_GRANULARITIES
    )


class TabularFormat(BaseFileFormat):
    extensions = ("csv", "xlsx", "xls")
    meta = FormatMeta(
        FormatCategory.SHEET, ContentStructure.TABULAR, _G.BY_ROW, (_G.BY_ROW,)
    )


class StructuredFormat(BaseFileFormat):
    extensions = ("json", "yaml", "xml")
    meta = FormatMeta(
        FormatCategory.CODE,
        ContentStructure.STRUCTURED,
        _G.BY_NODE,
        _NODE_GRANULARITIES,
    )


class CodeFormat(BaseFileFormat):
    extensions = ("py",)
    meta = FormatMeta(
        FormatCategory.CODE, ContentStructure.CODE, _G.WHOLE, _CODE_GRANULARITIES
    )


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
