from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from comet_rag.content import ContentItem
from comet_rag.documents.schemas import _freeze
from comet_rag.identity import stable_id


def vector_values(values: Sequence[float], dimension: int) -> tuple[float, ...]:
    if len(values) != dimension or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in values
    ):
        raise ValueError(
            "vector must have the configured dimension and finite numeric values"
        )
    return tuple(float(value) for value in values)


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    model: str
    dimension: int
    deployment: str
    version: str | None = None
    metric: Literal["COSINE", "IP", "L2"] = "COSINE"
    normalized: bool = False
    query_prefix: str = ""
    document_prefix: str = ""
    preprocessing: Mapping[str, object] = field(default_factory=dict)
    space_id: str = field(init=False)

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.model, self.deployment)
        ):
            raise ValueError("model and deployment must be nonblank")
        if type(self.dimension) is not int or self.dimension < 1:
            raise ValueError("dimension must be positive")
        if self.version is not None and (
            not isinstance(self.version, str) or not self.version
        ):
            raise ValueError("version must be nonempty or unknown")
        if (
            self.metric not in ("COSINE", "IP", "L2")
            or type(self.normalized) is not bool
        ):
            raise ValueError("invalid space configuration")
        if (
            not isinstance(self.query_prefix, str)
            or not isinstance(self.document_prefix, str)
            or not isinstance(self.preprocessing, Mapping)
        ):
            raise ValueError("invalid preprocessing configuration")
        object.__setattr__(self, "preprocessing", _freeze(self.preprocessing))
        object.__setattr__(
            self,
            "space_id",
            stable_id(
                "space",
                {
                    "model": self.model,
                    "dimension": self.dimension,
                    "deployment": self.deployment,
                    "version": self.version,
                    "metric": self.metric,
                    "normalized": self.normalized,
                    "query_prefix": self.query_prefix,
                    "document_prefix": self.document_prefix,
                    "preprocessing": self.preprocessing,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    item_id: str
    vector: tuple[float, ...]
    space_id: str

    def __post_init__(self) -> None:
        if not self.item_id or not self.space_id or not self.vector:
            raise ValueError("embedding identities and vector must be nonempty")
        object.__setattr__(self, "vector", vector_values(self.vector, len(self.vector)))


class AsyncContentEmbedder(Protocol):
    @property
    def space(self) -> EmbeddingSpace: ...

    async def embed(
        self, items: Sequence[ContentItem], *, role: Literal["query", "document"]
    ) -> tuple[EmbeddingResult, ...]: ...
