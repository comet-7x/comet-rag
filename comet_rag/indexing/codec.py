from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from comet_rag.chunkers import SourceRef
from comet_rag.content import ImagePart, TextPart
from comet_rag.documents.codec import _decode, _encode
from comet_rag.identity import canonical_json

from .types import IndexUnit, Representation


def to_dict(value: IndexUnit | Representation) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "unit" if isinstance(value, IndexUnit) else "representation",
        "schema_version": 1,
    }
    for item in fields(value):
        entry = getattr(value, item.name)
        if item.name == "content":
            result[item.name] = [
                {"type": "image_part", "resource": _encode(part.resource)}
                if isinstance(part, ImagePart)
                else {"type": "text_part", "text": part.text}
                for part in entry
            ]
        elif item.name in ("source_refs", "context_refs"):
            result[item.name] = [
                {
                    "document_id": ref.document_id,
                    "block_id": ref.block_id,
                    "projection_version": ref.projection_version,
                    "start": ref.start,
                    "end": ref.end,
                }
                for ref in entry
            ]
        else:
            result[item.name] = _encode(entry)
    canonical_json(result)
    return result


def from_dict(value: Mapping[str, Any]) -> IndexUnit | Representation:
    data = dict(value)
    tag = data.pop("type", None)
    version = data.pop("schema_version", None)
    if (
        type(version) is not int
        or version != 1
        or tag not in ("unit", "representation")
    ):
        raise ValueError("unsupported indexing type/version")
    cls = IndexUnit if tag == "unit" else Representation
    if set(data) != {item.name for item in fields(cls)}:
        raise ValueError("unknown or missing indexing fields")
    identities = {
        item.name: data.pop(item.name) for item in fields(cls) if not item.init
    }
    if not isinstance(data["content"], list):
        raise ValueError("content must be an array")
    parts = []
    for part in data["content"]:
        if not isinstance(part, dict):
            raise ValueError("invalid content part")
        if set(part) == {"type", "text"} and part["type"] == "text_part":
            parts.append(TextPart(part["text"]))
        elif set(part) == {"type", "resource"} and part["type"] == "image_part":
            parts.append(ImagePart(_decode(part["resource"], "media")))
        else:
            raise ValueError("invalid content part tag or fields")
    data["content"] = tuple(parts)
    for name in ("source_refs", "context_refs"):
        if name in data:
            if not isinstance(data[name], list):
                raise ValueError("source refs must be an array")
            names = {item.name for item in fields(SourceRef)}
            if any(
                not isinstance(ref, dict) or set(ref) != names for ref in data[name]
            ):
                raise ValueError("invalid source ref fields")
            data[name] = tuple(SourceRef(**ref) for ref in data[name])
    for name, expected in (
        ("assets", "asset"),
        ("locations", "location"),
        ("blocks", "block"),
    ):
        if name in data:
            if not isinstance(data[name], list):
                raise ValueError(f"{name} must be an array")
            data[name] = tuple(_decode(item, expected) for item in data[name])
    try:
        result = cls(**data)
    except TypeError as exc:
        raise ValueError("invalid indexing fields") from exc
    if any(getattr(result, name) != expected for name, expected in identities.items()):
        raise ValueError("indexing identity does not match content")
    return result
