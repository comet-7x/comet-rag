from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

from comet_rag.indexing.codec import from_dict, to_dict
from comet_rag.indexing.types import IndexUnit, Representation
from comet_rag.vectorstores.types import VectorConfirmation

from .types import (
    WriteManifest,
    content_digest,
    manifest_from_dict,
    manifest_to_dict,
    validate_write,
)


class AsyncDocStore(Protocol):
    @property
    def index_scope(self) -> str: ...

    async def upsert_units(
        self,
        units: Sequence[IndexUnit],
        *,
        representations: Sequence[Representation],
        manifest: WriteManifest,
    ) -> None: ...

    async def get_units(self, ids: Sequence[str]) -> dict[str, IndexUnit]: ...

    async def get_representations(
        self, ids: Sequence[str]
    ) -> dict[str, Representation]: ...

    async def get_manifest(self, document_id: str) -> WriteManifest | None: ...

    def iter_manifests(
        self, document_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[WriteManifest]: ...

    async def record_vector_confirmation(
        self, document_id: str, confirmation: VectorConfirmation
    ) -> None: ...

    async def delete_units(self, ids: Sequence[str]) -> None: ...

    async def delete_manifest(self, document_id: str) -> None: ...


def _scope(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("index_scope must be a string")
    if not value.strip():
        raise ValueError("index_scope must be nonblank")
    return value


def _ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("IDs must be a sequence of nonblank strings")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("IDs must be a sequence of nonblank strings")
    return tuple(dict.fromkeys(values))


def _batch_size(value: int) -> None:
    if type(value) is not int:
        raise ValueError("batch_size must be an integer")
    if value <= 0:
        raise ValueError("batch_size must be a positive integer")


def _snapshot(
    index_scope: str,
    units: Sequence[IndexUnit],
    representations: Sequence[Representation],
    manifest: WriteManifest,
) -> tuple[tuple[IndexUnit, ...], tuple[Representation, ...], WriteManifest]:
    stored_units: list[IndexUnit] = []
    stored_representations: list[Representation] = []
    for unit in units:
        value = from_dict(to_dict(unit))
        if not isinstance(value, IndexUnit):
            raise ValueError("invalid unit type")
        stored_units.append(value)
    for rep in representations:
        value = from_dict(to_dict(rep))
        if not isinstance(value, Representation):
            raise ValueError("invalid representation type")
        stored_representations.append(value)
    snapshot = manifest_from_dict(manifest_to_dict(manifest))
    validate_write(index_scope, stored_units, stored_representations, snapshot)
    return tuple(stored_units), tuple(stored_representations), snapshot


def _content_digest(value: IndexUnit | Representation) -> str:
    return (
        content_digest((value,), ())
        if isinstance(value, IndexUnit)
        else content_digest((), (value,))
    )


def _content_payload(value: IndexUnit | Representation) -> dict[str, Any]:
    return {
        **to_dict(value),
        "snapshot_version": 1,
        "snapshot_fingerprint": _content_digest(value),
    }


def _check_content(value: IndexUnit | Representation, fingerprint: object) -> None:
    if not isinstance(fingerprint, str):
        raise ValueError("missing snapshot fingerprint; rebuild in a new index_scope")
    if not fingerprint:
        raise ValueError("missing snapshot fingerprint; rebuild in a new index_scope")
    if _content_digest(value) != fingerprint:
        raise ValueError("stored snapshot fingerprint mismatch")


def _decode_content(payload: Mapping[str, Any]) -> IndexUnit | Representation:
    data = dict(payload)
    version = data.pop("snapshot_version", None)
    fingerprint = data.pop("snapshot_fingerprint", None)
    if type(version) is not int:
        raise ValueError("unsupported snapshot version; rebuild in a new index_scope")
    if version != 1:
        raise ValueError("unsupported snapshot version; rebuild in a new index_scope")
    value = from_dict(data)
    _check_content(value, fingerprint)
    return value


def _validate_manifest_match(existing: WriteManifest, incoming: WriteManifest) -> None:
    if existing.manifest_id != incoming.manifest_id:
        raise ValueError("immutable manifest conflict; rebuild in a new index_scope")
    if existing.content_fingerprint != incoming.content_fingerprint:
        raise ValueError("immutable manifest conflict; rebuild in a new index_scope")


def _validate_unit_parents(units: Mapping[str, IndexUnit]) -> None:
    for unit in units.values():
        path = {unit.unit_id}
        child = unit
        while child.parent_unit_id is not None:
            parent = units.get(child.parent_unit_id)
            if parent is None:
                raise ValueError("missing parent or unit cycle")
            if parent.unit_id in path:
                raise ValueError("missing parent or unit cycle")
            if (
                parent.index_scope,
                parent.source_id,
                parent.document_id,
                parent.index_profile_id,
            ) != (
                child.index_scope,
                child.source_id,
                child.document_id,
                child.index_profile_id,
            ):
                raise ValueError("parent identity mismatch")
            path.add(parent.unit_id)
            child = parent
