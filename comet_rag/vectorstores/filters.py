from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from comet_rag.indexing.types import IndexUnit
from comet_rag.vectorstores.types import VectorRecord

type Scalar = str | int | float | bool
_SYSTEM_FIELDS = {"source_id": str, "document_id": str, "unit_id": str}
_RESERVED_FIELDS = frozenset(
    (
        *_SYSTEM_FIELDS,
        "record_id",
        "representation_id",
        "index_scope",
        "index_profile_id",
        "space_id",
        "fingerprint",
    )
)


@dataclass(frozen=True, slots=True)
class Eq:
    field: str
    value: Scalar


@dataclass(frozen=True, slots=True)
class In:
    field: str
    values: Sequence[Scalar]

    def __post_init__(self) -> None:
        if isinstance(self.values, (str, bytes)):
            raise ValueError("In values must be a sequence of scalars")
        if not isinstance(self.values, Sequence):
            raise ValueError("In values must be a sequence of scalars")
        object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True, slots=True)
class And:
    clauses: Sequence[Filter]

    def __post_init__(self) -> None:
        if isinstance(self.clauses, (str, bytes)):
            raise ValueError("And clauses must be a sequence of filters")
        if not isinstance(self.clauses, Sequence):
            raise ValueError("And clauses must be a sequence of filters")
        object.__setattr__(self, "clauses", tuple(self.clauses))


type Filter = Eq | In | And


def _field_types(config: Mapping[str, type] | None) -> Mapping[str, type]:
    if config is not None:
        if not isinstance(config, Mapping):
            raise ValueError("field_types must be a mapping")
    fields: dict[str, type] = dict(_SYSTEM_FIELDS)
    for name, kind in (config or {}).items():
        if not isinstance(name, str):
            raise ValueError(
                "metadata filter fields must be nonempty and cannot replace identity fields"
            )
        if not name:
            raise ValueError(
                "metadata filter fields must be nonempty and cannot replace identity fields"
            )
        if name in _RESERVED_FIELDS:
            raise ValueError(
                "metadata filter fields must be nonempty and cannot replace identity fields"
            )
        if kind not in (str, int, float, bool):
            raise ValueError("metadata filter types must be str, int, float or bool")
        fields[name] = kind
    return MappingProxyType(fields)


def _scalar(value: object, kind: type) -> None:
    if type(value) is not kind:
        raise ValueError(f"filter values must have exact type {kind.__name__}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"filter values must have exact type {kind.__name__}")


def validate_filter(filters: Filter | None, fields: Mapping[str, type]) -> None:
    if filters is None:
        return
    if isinstance(filters, And):
        for clause in filters.clauses:
            if clause is None:
                raise ValueError("And clauses must be filters")
            validate_filter(clause, fields)
        return
    if not isinstance(filters, (Eq, In)):
        raise ValueError("unsupported filter")
    if not isinstance(filters.field, str):
        raise ValueError("unknown filter field")
    if filters.field not in fields:
        raise ValueError("unknown filter field")
    values = (filters.value,) if isinstance(filters, Eq) else filters.values
    for value in values:
        _scalar(value, fields[filters.field])


def validate_metadata(record: VectorRecord, fields: Mapping[str, type]) -> None:
    for name, value in record.metadata.items():
        if name in _RESERVED_FIELDS:
            raise ValueError("metadata cannot replace record identity fields")
        if name in fields:
            _scalar(value, fields[name])


def matches(record: VectorRecord | IndexUnit, filters: Filter | None) -> bool:
    if filters is None:
        return True
    if isinstance(filters, And):
        return all(matches(record, clause) for clause in filters.clauses)
    value = (
        getattr(record, filters.field)
        if filters.field in _SYSTEM_FIELDS
        else record.metadata.get(filters.field)
    )
    if isinstance(filters, Eq):
        return type(value) is type(filters.value) and value == filters.value
    return any(
        type(value) is type(expected) and value == expected
        for expected in filters.values
    )


def expression(filters: Filter | None) -> str:
    """仅渲染已完整验证的过滤；字段路径和值均使用 JSON 字面量。"""
    if filters is None:
        return ""
    if isinstance(filters, And):
        parts = [expression(clause) for clause in filters.clauses]
        return " and ".join(f"({part})" for part in parts if part)
    field = (
        filters.field
        if filters.field in _SYSTEM_FIELDS
        else f"metadata[{json.dumps(filters.field, ensure_ascii=False)}]"
    )
    if isinstance(filters, Eq):
        return f"{field} == {json.dumps(filters.value, ensure_ascii=False, allow_nan=False)}"
    if not filters.values:
        return 'record_id in [""]'
    return f"{field} in {json.dumps(list(filters.values), ensure_ascii=False, allow_nan=False)}"


__all__ = ["And", "Eq", "Filter", "In"]
