"""带显式类型与版本的 JSON；拒绝未知字段，绝不隐式迁移。"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, cast

from comet_rag.content import MediaRef
from comet_rag.identity import canonical_json

from . import schemas as s

_TAGS = {
    s.SourceInfo: "source",
    s.ParsingInfo: "parsing",
    s.TextStyle: "style",
    s.TextRun: "text_run",
    s.InlineCode: "inline_code",
    s.FormulaContent: "formula_content",
    s.InlineFormula: "inline_formula",
    s.RichText: "rich_text",
    s.Location: "location",
    s.Asset: "asset",
    s.TableCell: "cell",
    s.TableContent: "table_content",
    s.BlockRelation: "relation",
    s.PageInfo: "page",
    s.ParsedDocument: "document",
    s.ParseIssue: "issue",
    s.ParseReport: "report",
    s.ParseResult: "result",
    MediaRef: "media",
}
_TYPES = {tag: cls for cls, tag in _TAGS.items()}
_NESTED = {
    "source": "source",
    "parsing": "parsing",
    "style": "style",
    "formula": "formula_content",
    "rich_text": "rich_text",
    "media": "media",
    "table": "table_content",
    "document": "document",
    "report": "report",
}
_SEQUENCES = {
    "locations": "location",
    "blocks": "block",
    "assets": "asset",
    "pages": "page",
    "cells": "cell",
    "relations": "relation",
    "issues": "issue",
    "inlines": "inline",
}


def _encode(value: object, *, snapshot: bool = False) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        result = {"type": "block" if isinstance(value, s.Block) else _TAGS[type(value)]}
        for item in fields(value):
            if snapshot and (
                item.name in {"document_id", "metadata"}
                or isinstance(value, s.Asset)
                and item.name in {"media", "name"}
                or isinstance(value, s.SourceInfo)
                and item.name == "locator"
            ):
                continue
            if (
                isinstance(value, MediaRef)
                and item.name == "url"
                and value.url is not None
            ):
                s._safe_locator(value.url)
            result[item.name] = _encode(getattr(value, item.name), snapshot=snapshot)
        return result
    if isinstance(value, Mapping):
        return {key: _encode(item, snapshot=snapshot) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item, snapshot=snapshot) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _decode(value: object, expected: str) -> Any:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected tagged object")
    data: dict[str, Any] = dict(value)
    tag = data.pop("type", None)
    if not isinstance(tag, str):
        raise ValueError("type tag must be a string")
    if expected == "inline":
        if tag not in {"text_run", "inline_code", "inline_formula"}:
            raise ValueError("unsupported inline tag")
    elif tag != expected:
        raise ValueError(f"expected {expected} tag")
    cls = s.Block if tag == "block" else _TYPES.get(tag)
    if cls is None:
        raise ValueError("unknown type tag")
    known = {item.name for item in fields(cls)}
    if set(data) - known:
        raise ValueError(f"unknown {tag} fields: {sorted(set(data) - known)}")
    supplied_id = data.pop("document_id", None) if cls is s.ParsedDocument else None
    if cls is s.ParsedDocument and (
        "schema_version" not in data
        or "projection_version" not in data
        or supplied_id is None
    ):
        raise ValueError("document requires explicit versions and snapshot identity")
    for name, item in list(data.items()):
        if item is None:
            continue
        if name in {"config", "metadata", "layout", "coverage"}:
            if not isinstance(item, dict):
                raise ValueError(f"{name} must be a JSON object")
            canonical_json(item)
        elif name in _NESTED:
            data[name] = _decode(item, _NESTED[name])
        elif name in _SEQUENCES:
            if not isinstance(item, list):
                raise ValueError(f"{name} must be an array")
            data[name] = tuple(_decode(member, _SEQUENCES[name]) for member in item)
    if cls is MediaRef:
        if any(
            data.get(name) is not None and not isinstance(data[name], str)
            for name in ("path", "url", "data", "media_type")
        ):
            raise ValueError("media values must be strings or null")
        if data.get("url") is not None:
            s._safe_locator(data["url"])
        if data.get("data") is not None:
            try:
                data["data"] = base64.b64decode(data["data"], validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid base64 media") from exc
    try:
        result = cls(**data)
    except TypeError as exc:
        raise ValueError(f"invalid {tag} fields") from exc
    if isinstance(result, s.ParsedDocument) and result.document_id != supplied_id:
        raise ValueError("document_id does not match snapshot")
    return result


def document_to_dict(document: s.ParsedDocument) -> dict[str, object]:
    return _encode(document)


def document_from_dict(value: Mapping[str, object]) -> s.ParsedDocument:
    return cast(s.ParsedDocument, _decode(dict(value), "document"))


def dumps(value: s.ParsedDocument | s.ParseResult) -> str:
    return canonical_json(_encode(value)).decode("utf-8")


def loads(value: str | bytes) -> s.ParsedDocument | s.ParseResult:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = item
        return result

    def invalid_number(token: str) -> None:
        raise ValueError(f"invalid JSON number: {token}")

    data = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_number)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("type"), str)
        or data["type"] not in {"document", "result"}
    ):
        raise ValueError("expected document or result")
    return _decode(data, data["type"])
