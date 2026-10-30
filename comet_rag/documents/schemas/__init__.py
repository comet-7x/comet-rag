"""不可变的规范化文档；所有位置与引用绑定同一快照。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Literal

from comet_rag._urls import has_url_credentials
from comet_rag.content import MediaRef
from comet_rag.identity import stable_id

SCHEMA_VERSION = PROJECTION_VERSION = 1
TEXT_KINDS = frozenset({"text", "paragraph", "heading", "list_item"})
BLOCK_KINDS = TEXT_KINDS | {"image", "code", "formula", "table"}


def _validate_text(value: object, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not empty:
        if not value:
            raise ValueError(f"{name} must be a nonempty string")


def _validate_integer(value: object, name: str, minimum: int = 0) -> None:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _hash(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("content_hash must be lowercase SHA-256 hex")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("content_hash must be lowercase SHA-256 hex")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if (
        value is None
        or type(value) in (bool, int, str)
        or (type(value) is float and math.isfinite(value))
    ):
        return value
    raise ValueError("expected finite JSON values")


def _freeze_json_field(owner: object, name: str) -> None:
    value = getattr(owner, name)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    object.__setattr__(owner, name, _freeze(value))


def _freeze_sequence(
    owner: object, name: str, item_type: type | tuple[type, ...]
) -> None:
    value = getattr(owner, name)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"invalid {name}")
    if any(not isinstance(item, item_type) for item in value):
        raise ValueError(f"invalid {name}")
    object.__setattr__(owner, name, tuple(value))


def _safe_locator(value: str) -> None:
    _validate_text(value, "locator")
    if has_url_credentials(value):
        raise ValueError(
            "persistent locator cannot contain credentials or signed access parameters"
        )


@dataclass(frozen=True, slots=True)
class SourceInfo:
    source_id: str
    content_hash: str
    locator: str | None = None
    source_version: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.source_id, "source_id")
        _hash(self.content_hash)
        if self.locator is not None:
            _safe_locator(self.locator)
        if self.source_version is not None:
            _validate_text(self.source_version, "source_version")


@dataclass(frozen=True, slots=True)
class ParsingInfo:
    engine: str
    version: str | None = None
    config: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_text(self.engine, "engine")
        if self.version is not None:
            _validate_text(self.version, "engine version")
        _freeze_json_field(self, "config")


@dataclass(frozen=True, slots=True)
class TextStyle:
    font: str | None = None
    size_pt: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    color: str | None = None
    script: Literal["normal", "superscript", "subscript"] | None = None

    def __post_init__(self) -> None:
        if self.font is not None:
            _validate_text(self.font, "font")
        if self.size_pt is not None:
            if type(self.size_pt) not in (int, float):
                raise ValueError("size_pt must be finite and positive")
            if not math.isfinite(self.size_pt):
                raise ValueError("size_pt must be finite and positive")
            if self.size_pt <= 0:
                raise ValueError("size_pt must be finite and positive")
        for name in ("bold", "italic", "underline", "strike"):
            value = getattr(self, name)
            if value is not None:
                if type(value) is not bool:
                    raise ValueError(f"{name} must be bool or None")
        if self.color is not None:
            if not isinstance(self.color, str):
                raise ValueError("color must be #RRGGBB")
            if re.fullmatch(r"#[0-9A-Fa-f]{6}", self.color) is None:
                raise ValueError("color must be #RRGGBB")
            object.__setattr__(self, "color", self.color.upper())
        if self.script not in (None, "normal", "superscript", "subscript"):
            raise ValueError("invalid script")


@dataclass(frozen=True, slots=True)
class TextRun:
    text: str
    style: TextStyle | None = None
    link: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.text, "text", empty=True)
        if self.style is not None:
            if not isinstance(self.style, TextStyle):
                raise ValueError("invalid text style")
        if self.link is not None:
            _safe_locator(self.link)


@dataclass(frozen=True, slots=True)
class InlineCode:
    text: str

    def __post_init__(self) -> None:
        _validate_text(self.text, "code", empty=True)


@dataclass(frozen=True, slots=True)
class FormulaContent:
    expression: str | None = None
    format: Literal["latex", "mathml", "omml"] | None = None
    asset_id: str | None = None

    def __post_init__(self) -> None:
        if self.expression is not None:
            _validate_text(self.expression, "expression")
        if self.format not in (None, "latex", "mathml", "omml"):
            raise ValueError("formula expression requires a supported format")
        if (self.expression is None) != (self.format is None):
            raise ValueError("formula expression requires a supported format")
        if self.asset_id is not None:
            _validate_text(self.asset_id, "formula asset_id")
        if self.expression is None:
            if self.asset_id is None:
                raise ValueError("formula requires an expression or asset")


@dataclass(frozen=True, slots=True)
class InlineFormula:
    formula: FormulaContent

    def __post_init__(self) -> None:
        if not isinstance(self.formula, FormulaContent):
            raise ValueError("invalid inline formula")


@dataclass(frozen=True, slots=True)
class RichText:
    inlines: tuple[TextRun | InlineCode | InlineFormula, ...] = ()

    def __post_init__(self) -> None:
        _freeze_sequence(self, "inlines", (TextRun, InlineCode, InlineFormula))


@dataclass(frozen=True, slots=True)
class Location:
    page_id: str | None = None
    element_id: str | None = None
    start: int | None = None
    end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    unit: Literal["pt", "px", "normalized"] | None = None
    origin: Literal["top-left", "bottom-left"] | None = None
    coordinate_space: Literal["source"] = "source"
    coordinate_version: int = 1

    def __post_init__(self) -> None:
        for value in (self.page_id, self.element_id):
            if value is not None:
                _validate_text(value, "location reference")
        if self.coordinate_space != "source":
            raise ValueError("unsupported source coordinate version/space")
        if type(self.coordinate_version) is not int:
            raise ValueError("unsupported source coordinate version/space")
        if self.coordinate_version != 1:
            raise ValueError("unsupported source coordinate version/space")
        if (self.start is None) != (self.end is None):
            raise ValueError("source range requires start and end")
        if self.start is not None and self.end is not None:
            _validate_integer(self.start, "start")
            _validate_integer(self.end, "end", self.start)
        if self.bbox is None:
            if self.unit is not None:
                raise ValueError("coordinate unit/origin require bbox")
            if self.origin is not None:
                raise ValueError("coordinate unit/origin require bbox")
        else:
            _freeze_sequence(self, "bbox", (int, float))
            if len(self.bbox) != 4:
                raise ValueError("bbox must contain four finite nonnegative numbers")
            if any(
                type(x) not in (int, float) or not math.isfinite(x) or x < 0
                for x in self.bbox
            ):
                raise ValueError("bbox must contain four finite nonnegative numbers")
            x0, y0, x1, y1 = self.bbox
            if not self.page_id:
                raise ValueError(
                    "bbox requires page, unit, origin and ordered coordinates"
                )
            if self.unit not in ("pt", "px", "normalized"):
                raise ValueError(
                    "bbox requires page, unit, origin and ordered coordinates"
                )
            if self.origin not in ("top-left", "bottom-left"):
                raise ValueError(
                    "bbox requires page, unit, origin and ordered coordinates"
                )
            if x1 < x0:
                raise ValueError(
                    "bbox requires page, unit, origin and ordered coordinates"
                )
            if y1 < y0:
                raise ValueError(
                    "bbox requires page, unit, origin and ordered coordinates"
                )
            if self.unit == "normalized":
                if max(self.bbox) > 1:
                    raise ValueError(
                        "bbox requires page, unit, origin and ordered coordinates"
                    )


@dataclass(frozen=True, slots=True)
class Asset:
    asset_id: str
    content_hash: str
    media: MediaRef
    media_type: str | None = None
    name: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    provenance: str = "extracted"
    source_asset_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_text(self.asset_id, "asset_id")
        _hash(self.content_hash)
        _validate_text(self.provenance, "provenance")
        if not isinstance(self.media, MediaRef):
            raise ValueError("invalid media reference")
        if self.media.data is not None:
            if sha256(self.media.data).hexdigest() != self.content_hash:
                raise ValueError("asset bytes do not match content_hash")
        if self.media_type is None:
            object.__setattr__(self, "media_type", self.media.media_type)
        if self.media_type is not None:
            _validate_text(self.media_type, "media_type")
            if self.media.media_type is not None:
                if self.media_type != self.media.media_type:
                    raise ValueError("conflicting media types")
        if self.name is not None:
            _validate_text(self.name, "asset name")
        for name in ("size_bytes", "width", "height"):
            value = getattr(self, name)
            if value is not None:
                _validate_integer(value, name, 0 if name == "size_bytes" else 1)
        if (self.width is None) != (self.height is None):
            raise ValueError("pixel dimensions require width and height")
        if self.media.data is not None:
            if self.size_bytes is not None:
                if self.size_bytes != len(self.media.data):
                    raise ValueError("asset size does not match bytes")
        _freeze_sequence(self, "source_asset_ids", str)


@dataclass(frozen=True, slots=True)
class TableCell:
    row: int
    column: int
    block_ids: tuple[str, ...] = ()
    row_span: int = 1
    column_span: int = 1
    header: bool | None = None
    locations: tuple[Location, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.row, self.column):
            _validate_integer(value, "cell coordinate")
        for value in (self.row_span, self.column_span):
            _validate_integer(value, "cell span", 1)
        if self.header is not None:
            if type(self.header) is not bool:
                raise ValueError("header must be bool or None")
        _freeze_sequence(self, "block_ids", str)
        _freeze_sequence(self, "locations", Location)


@dataclass(frozen=True, slots=True)
class TableContent:
    representation: Literal["cells", "html", "image"] = "cells"
    cells: tuple[TableCell, ...] | None = None
    html: str | None = None
    asset_id: str | None = None
    rows: int | None = None
    columns: int | None = None

    def __post_init__(self) -> None:
        if self.representation not in ("cells", "html", "image"):
            raise ValueError("table requires exactly one tagged primary representation")
        if (
            sum(value is not None for value in (self.cells, self.html, self.asset_id))
            != 1
        ):
            raise ValueError("table requires exactly one tagged primary representation")
        tagged = {"cells": self.cells, "html": self.html, "image": self.asset_id}[
            self.representation
        ]
        if tagged is None:
            raise ValueError("table requires exactly one tagged primary representation")
        for value in (self.rows, self.columns):
            if value is not None:
                _validate_integer(value, "table dimensions")
        if self.html is not None:
            _validate_text(self.html, "table HTML")
        if self.asset_id is not None:
            _validate_text(self.asset_id, "table asset_id")
        if self.cells is not None:
            _freeze_sequence(self, "cells", TableCell)
            for i, cell in enumerate(self.cells):
                if self.rows is not None:
                    if cell.row + cell.row_span > self.rows:
                        raise ValueError("cell exceeds table dimensions")
                if self.columns is not None:
                    if cell.column + cell.column_span > self.columns:
                        raise ValueError("cell exceeds table dimensions")
                if any(
                    cell.row < prior.row + prior.row_span
                    and prior.row < cell.row + cell.row_span
                    and cell.column < prior.column + prior.column_span
                    and prior.column < cell.column + cell.column_span
                    for prior in self.cells[:i]
                ):
                    raise ValueError("overlapping table cells")


@dataclass(frozen=True, slots=True)
class BlockRelation:
    kind: str
    target_block_id: str

    def __post_init__(self) -> None:
        _validate_text(self.kind, "relation kind")
        _validate_text(self.target_block_id, "relation target")


@dataclass(frozen=True, slots=True)
class Block:
    block_id: str
    kind: Literal[
        "text", "paragraph", "heading", "list_item", "table", "image", "code", "formula"
    ]
    rich_text: RichText | None = None
    code: str | None = None
    formula: FormulaContent | None = None
    asset_id: str | None = None
    table: TableContent | None = None
    parent_block_id: str | None = None
    role: str | None = None
    locations: tuple[Location, ...] = ()
    relations: tuple[BlockRelation, ...] = ()
    level: int | None = None
    marker: str | None = None
    language: str | None = None
    filename: str | None = None
    alt_text: str | None = None
    formula_number: str | None = None
    layout: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_text(self.block_id, "block_id")
        payload = {
            "rich_text": self.rich_text,
            "code": self.code,
            "formula": self.formula,
            "asset_id": self.asset_id,
            "table": self.table,
        }
        expected = (
            "rich_text"
            if self.kind in TEXT_KINDS
            else {
                "image": "asset_id",
                "code": "code",
                "formula": "formula",
                "table": "table",
            }.get(self.kind)
        )
        if expected is None:
            raise ValueError("block requires exactly its kind's payload")
        if payload[expected] is None:
            raise ValueError("block requires exactly its kind's payload")
        if sum(value is not None for value in payload.values()) != 1:
            raise ValueError("block requires exactly its kind's payload")
        if self.rich_text is not None:
            if not isinstance(self.rich_text, RichText):
                raise ValueError("invalid block payload")
        if self.formula is not None:
            if not isinstance(self.formula, FormulaContent):
                raise ValueError("invalid block payload")
        if self.table is not None:
            if not isinstance(self.table, TableContent):
                raise ValueError("invalid block payload")
        for value in (self.code, self.alt_text, self.marker):
            if value is not None:
                _validate_text(value, "block text", empty=True)
        for value in (
            self.asset_id,
            self.parent_block_id,
            self.role,
            self.language,
            self.filename,
            self.formula_number,
        ):
            if value is not None:
                _validate_text(value, "block attribute")
        if self.level is not None:
            _validate_integer(self.level, "level", 1 if self.kind == "heading" else 0)
        for valid, value in (
            (self.kind in {"heading", "list_item"}, self.level),
            (self.kind == "list_item", self.marker),
            (self.kind == "code", self.language),
            (self.kind == "code", self.filename),
            (self.kind == "image", self.alt_text),
            (self.kind == "formula", self.formula_number),
        ):
            if value is not None:
                if not valid:
                    raise ValueError("attribute incompatible with block kind")
        _freeze_sequence(self, "locations", Location)
        _freeze_sequence(self, "relations", BlockRelation)
        _freeze_json_field(self, "layout")
        _freeze_json_field(self, "metadata")


@dataclass(frozen=True, slots=True)
class PageInfo:
    page_id: str
    kind: Literal["page", "slide"] = "page"
    number: int | None = None
    width: float | None = None
    height: float | None = None
    unit: Literal["pt", "px"] | None = None
    asset_id: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.page_id, "page_id")
        if self.kind not in ("page", "slide"):
            raise ValueError("invalid page kind")
        if self.number is not None:
            _validate_integer(self.number, "page number", 1)
        if self.asset_id is not None:
            _validate_text(self.asset_id, "page asset_id")
        if any(value is not None for value in (self.width, self.height, self.unit)):
            if self.unit not in ("pt", "px"):
                raise ValueError(
                    "page dimensions require positive width, height and unit"
                )
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
                for value in (self.width, self.height)
            ):
                raise ValueError(
                    "page dimensions require positive width, height and unit"
                )


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source: SourceInfo
    parsing: ParsingInfo
    blocks: tuple[Block, ...] = ()
    assets: tuple[Asset, ...] = ()
    pages: tuple[PageInfo, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    projection_version: int = PROJECTION_VERSION
    document_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise ValueError("unsupported schema/projection version")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema/projection version")
        if type(self.projection_version) is not int:
            raise ValueError("unsupported schema/projection version")
        if self.projection_version != PROJECTION_VERSION:
            raise ValueError("unsupported schema/projection version")
        if not isinstance(self.source, SourceInfo):
            raise ValueError("invalid source/parsing")
        if not isinstance(self.parsing, ParsingInfo):
            raise ValueError("invalid source/parsing")
        for name, item_type in (
            ("blocks", Block),
            ("assets", Asset),
            ("pages", PageInfo),
        ):
            _freeze_sequence(self, name, item_type)
        _freeze_json_field(self, "metadata")
        blocks = {block.block_id: block for block in self.blocks}
        assets = {asset.asset_id: asset for asset in self.assets}
        pages = {page.page_id: page for page in self.pages}
        if len(blocks) != len(self.blocks):
            raise ValueError("duplicate block/asset/page IDs")
        if len(assets) != len(self.assets):
            raise ValueError("duplicate block/asset/page IDs")
        if len(pages) != len(self.pages):
            raise ValueError("duplicate block/asset/page IDs")
        order = {block.block_id: i for i, block in enumerate(self.blocks)}
        owned: set[str] = set()
        checked: set[str] = set()
        for block in self.blocks:
            path: set[str] = set()
            cursor: str | None = block.block_id
            while cursor is not None and cursor not in checked:
                if cursor not in blocks:
                    raise ValueError("missing parent block")
                if cursor in path:
                    raise ValueError("parent cycle")
                path.add(cursor)
                cursor = blocks[cursor].parent_block_id
            checked.update(path)
            if any(
                relation.target_block_id not in blocks for relation in block.relations
            ):
                raise ValueError("missing related block")
            if block.table is not None:
                if block.table.cells is not None:
                    for cell in block.table.cells:
                        if any(member not in blocks for member in cell.block_ids):
                            raise ValueError(
                                "cell member must exist and belong to the same table"
                            )
                        if any(
                            blocks[member].parent_block_id != block.block_id
                            for member in cell.block_ids
                        ):
                            raise ValueError(
                                "cell member must exist and belong to the same table"
                            )
                        if list(cell.block_ids) != sorted(
                            cell.block_ids, key=order.__getitem__
                        ):
                            raise ValueError("cell order conflicts with document order")
                        for member in cell.block_ids:
                            if member in owned:
                                raise ValueError("block belongs to multiple cells")
                            owned.add(member)
                        self._validate_page_locations(cell.locations, pages)
            self._validate_page_locations(block.locations, pages)
            references = [
                block.asset_id,
                block.formula.asset_id if block.formula else None,
                block.table.asset_id if block.table else None,
            ]
            if block.rich_text is not None:
                references.extend(
                    inline.formula.asset_id
                    for inline in block.rich_text.inlines
                    if isinstance(inline, InlineFormula)
                )
            if any(
                reference is not None and reference not in assets
                for reference in references
            ):
                raise ValueError("missing block asset")
        if any(
            block.parent_block_id is not None
            and blocks[block.parent_block_id].kind == "table"
            and block.block_id not in owned
            for block in self.blocks
        ):
            raise ValueError("table child must belong to a cell")
        if any(
            page.asset_id is not None and page.asset_id not in assets
            for page in self.pages
        ):
            raise ValueError("missing or self-referencing resource")
        for asset in self.assets:
            for ref in asset.source_asset_ids:
                if ref not in assets:
                    raise ValueError("missing or self-referencing resource")
                if ref == asset.asset_id:
                    raise ValueError("missing or self-referencing resource")
        from ..codec import _encode

        object.__setattr__(
            self, "document_id", stable_id("document", _encode(self, snapshot=True))
        )

    @staticmethod
    def _validate_page_locations(
        locations: tuple[Location, ...], pages: Mapping[str, PageInfo]
    ) -> None:
        for location in locations:
            if location.page_id is None:
                continue
            if location.page_id not in pages:
                raise ValueError("missing location page")
            page = pages[location.page_id]
            if location.bbox is None:
                continue
            if location.unit == "normalized":
                continue
            if page.unit is None:
                continue
            if location.unit != page.unit:
                raise ValueError("location and page units differ")


@dataclass(frozen=True, slots=True)
class ParseIssue:
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"
    locations: tuple[Location, ...] = ()

    def __post_init__(self) -> None:
        _validate_text(self.code, "issue code")
        _validate_text(self.message, "issue message")
        if self.severity not in ("info", "warning", "error"):
            raise ValueError("invalid issue severity")
        _freeze_sequence(self, "locations", Location)


@dataclass(frozen=True, slots=True)
class ParseReport:
    status: Literal["complete", "partial"] = "complete"
    issues: tuple[ParseIssue, ...] = ()
    coverage: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _freeze_sequence(self, "issues", ParseIssue)
        if self.status not in ("complete", "partial"):
            raise ValueError("invalid parse status or missing partial diagnostics")
        if self.status == "partial":
            if not self.issues:
                raise ValueError("invalid parse status or missing partial diagnostics")
        if self.status == "complete":
            if any(issue.severity == "error" for issue in self.issues):
                raise ValueError("invalid parse status or missing partial diagnostics")
        if self.coverage is not None:
            _freeze_json_field(self, "coverage")


@dataclass(frozen=True, slots=True)
class ParseResult:
    document: ParsedDocument
    report: ParseReport = field(default_factory=ParseReport)

    def __post_init__(self) -> None:
        if not isinstance(self.document, ParsedDocument):
            raise ValueError("invalid parse result")
        if not isinstance(self.report, ParseReport):
            raise ValueError("invalid parse result")
        for issue in self.report.issues:
            self.document._validate_page_locations(
                issue.locations, {page.page_id: page for page in self.document.pages}
            )
