from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path

type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)


def _json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite numbers have no canonical encoding")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported identity value: {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def stable_id(kind: str, value: object, *, version: int = 1) -> str:
    if not isinstance(kind, str) or re.fullmatch(r"[a-z][a-z0-9_]*", kind) is None:
        raise ValueError("identity kind must be a lowercase tag")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("identity version must be a positive integer")
    payload = canonical_json({"kind": kind, "version": version, "value": value})
    return f"{kind}_{hashlib.sha256(payload).hexdigest()}"


def file_hash(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
