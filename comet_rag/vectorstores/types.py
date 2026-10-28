from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from comet_rag.documents.schemas import _freeze
from comet_rag.embedder.types import EmbeddingSpace, vector_values
from comet_rag.identity import stable_id


@dataclass(frozen=True, slots=True)
class VectorRecord:
    representation_id: str
    unit_id: str
    source_id: str
    document_id: str
    index_scope: str
    index_profile_id: str
    space_id: str
    vector: tuple[float, ...]
    keyword_text: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    record_id: str = field(init=False)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for value in (
            self.representation_id,
            self.unit_id,
            self.source_id,
            self.document_id,
            self.index_scope,
            self.index_profile_id,
            self.space_id,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("record identity fields must be nonblank")
        if not isinstance(self.keyword_text, str) or not isinstance(
            self.metadata, Mapping
        ):
            raise ValueError("invalid keyword text or metadata")
        object.__setattr__(self, "vector", vector_values(self.vector, len(self.vector)))
        if not self.vector:
            raise ValueError("vector must be nonempty")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(
            self,
            "record_id",
            stable_id(
                "record",
                {
                    "scope": self.index_scope,
                    "representation": self.representation_id,
                    "space": self.space_id,
                },
            ),
        )
        object.__setattr__(
            self,
            "fingerprint",
            stable_id(
                "record_content",
                {
                    "representation": self.representation_id,
                    "unit": self.unit_id,
                    "source": self.source_id,
                    "document": self.document_id,
                    "scope": self.index_scope,
                    "profile": self.index_profile_id,
                    "space": self.space_id,
                    "vector": self.vector,
                    "text": self.keyword_text,
                    "metadata": self.metadata,
                },
            ),
        )

    def validate(
        self, *, index_scope: str, index_profile_id: str, space: EmbeddingSpace
    ) -> None:
        if (self.index_scope, self.index_profile_id, self.space_id) != (
            index_scope,
            index_profile_id,
            space.space_id,
        ):
            raise ValueError("record scope/profile/space mismatch")
        vector_values(self.vector, space.dimension)


@dataclass(frozen=True, slots=True)
class VectorHit:
    record_id: str
    representation_id: str
    unit_id: str
    source_id: str
    document_id: str
    index_scope: str
    index_profile_id: str
    space_id: str
    score: float
    metric: str
    higher_is_better: bool


@dataclass(frozen=True, slots=True)
class VectorConfirmation:
    record_ids: tuple[str, ...]
    accepted: bool = False
    durable: bool = False
    query_visible: bool = False
    unknown_ids: tuple[str, ...] = ()
    guarantee: str = "unknown"

    def __post_init__(self) -> None:
        for name in ("record_ids", "unknown_ids"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values) or len(
                values
            ) != len(set(values)):
                raise ValueError("confirmation IDs must be unique nonempty strings")
            object.__setattr__(self, name, values)
        if not set(self.unknown_ids) <= set(self.record_ids):
            raise ValueError("unknown IDs must belong to this confirmation")
        if any(
            type(value) is not bool
            for value in (self.accepted, self.durable, self.query_visible)
        ):
            raise ValueError("confirmation flags must be boolean")
        if self.query_visible and (not self.accepted or self.unknown_ids):
            raise ValueError("visible records require known acceptance")


@dataclass(frozen=True, slots=True)
class RecordVerification:
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    guarantee: str = "unknown"

    def __post_init__(self) -> None:
        values = (*self.present, *self.missing, *self.unknown)
        if len(values) != len(set(values)):
            raise ValueError("verification groups must be disjoint")
