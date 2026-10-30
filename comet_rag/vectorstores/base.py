from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Protocol

from comet_rag.embedder.types import EmbeddingResult, EmbeddingSpace, vector_values
from comet_rag.vectorstores.filters import Filter, validate_metadata
from comet_rag.vectorstores.types import (
    RecordVerification,
    VectorConfirmation,
    VectorHit,
    VectorRecord,
)


class AsyncVectorStore(Protocol):
    @property
    def index_scope(self) -> str: ...

    @property
    def index_profile_id(self) -> str: ...

    @property
    def space(self) -> EmbeddingSpace: ...

    async def prepare(self) -> None: ...

    async def upsert(self, records: Sequence[VectorRecord]) -> VectorConfirmation: ...

    async def search(
        self, query: EmbeddingResult, *, limit: int = 40, filters: Filter | None = None
    ) -> tuple[VectorHit, ...]: ...

    async def search_text(
        self, text: str, *, limit: int = 40, filters: Filter | None = None
    ) -> tuple[VectorHit, ...]: ...

    async def verify_records(
        self,
        record_ids: Sequence[str],
        *,
        fingerprints: Mapping[str, str] | None = None,
    ) -> RecordVerification: ...

    async def delete(self, record_ids: Sequence[str]) -> VectorConfirmation: ...


def _binding(index_scope: str, index_profile_id: str, space: EmbeddingSpace) -> None:
    for name, value in (
        ("index_scope", index_scope),
        ("index_profile_id", index_profile_id),
    ):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if not value.strip():
            raise ValueError(f"{name} must be nonblank")
    if not isinstance(space, EmbeddingSpace):
        raise ValueError("space must be an EmbeddingSpace")


def _snapshot_records(
    records: Sequence[VectorRecord],
    index_scope: str,
    index_profile_id: str,
    space: EmbeddingSpace,
    fields: Mapping[str, type],
) -> tuple[VectorRecord, ...]:
    snapshots: dict[str, VectorRecord] = {}
    for record in records:
        if not isinstance(record, VectorRecord):
            raise ValueError("records must be VectorRecord values")
        snapshot = replace(record)
        snapshot.validate(
            index_scope=index_scope, index_profile_id=index_profile_id, space=space
        )
        validate_metadata(snapshot, fields)
        previous = snapshots.get(snapshot.record_id)
        if previous is not None:
            if previous.fingerprint != snapshot.fingerprint:
                raise ValueError("conflicting content for the same record ID")
        snapshots[snapshot.record_id] = snapshot
    return tuple(snapshots.values())


def _ids(
    record_ids: Sequence[str], fingerprints: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    if isinstance(record_ids, (str, bytes)):
        raise ValueError("record_ids must be a sequence of IDs")
    values = tuple(record_ids)
    for value in values:
        if not isinstance(value, str):
            raise ValueError("record IDs must be nonblank strings")
        if not value.strip():
            raise ValueError("record IDs must be nonblank strings")
    if len(values) != len(set(values)):
        raise ValueError("record IDs must be unique")
    if fingerprints is None:
        return values
    if not isinstance(fingerprints, Mapping):
        raise ValueError(
            "fingerprints must contain a nonempty expected fingerprint for every ID"
        )
    if any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in fingerprints.items()
    ):
        raise ValueError(
            "fingerprints must contain a nonempty expected fingerprint for every ID"
        )
    if not set(values) <= fingerprints.keys():
        raise ValueError(
            "fingerprints must contain a nonempty expected fingerprint for every ID"
        )
    return values


def _query(query: EmbeddingResult, space: EmbeddingSpace) -> None:
    if not isinstance(query, EmbeddingResult):
        raise ValueError("query embedding space mismatch")
    if query.space_id != space.space_id:
        raise ValueError("query embedding space mismatch")
    vector_values(query.vector, space.dimension)


def _limit(limit: int) -> None:
    if type(limit) is not int:
        raise ValueError("limit must be an integer")
    if limit <= 0:
        raise ValueError("limit must be a positive integer")


def _hit(record: VectorRecord, score: float, metric: str) -> VectorHit:
    return VectorHit(
        record_id=record.record_id,
        representation_id=record.representation_id,
        unit_id=record.unit_id,
        source_id=record.source_id,
        document_id=record.document_id,
        index_scope=record.index_scope,
        index_profile_id=record.index_profile_id,
        space_id=record.space_id,
        score=score,
        metric=metric,
        higher_is_better=metric != "L2",
    )


__all__ = ["AsyncVectorStore"]
