from __future__ import annotations

from dataclasses import dataclass

from comet_rag.indexing.types import IndexUnit
from comet_rag.rerankers import RerankResult
from comet_rag.vectorstores.types import VectorHit


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    channel: str
    rank: int
    hit: VectorHit


@dataclass(frozen=True, slots=True)
class SearchMatch:
    unit: IndexUnit
    evidence: tuple[RetrievalEvidence, ...]
    fusion_score: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    matched_units: tuple[SearchMatch, ...]
    contexts: tuple[IndexUnit, ...]
    channels: tuple[str, ...]
    reranked: bool = False
    diagnostics: tuple[str, ...] = ()
    rerank_result: RerankResult | None = None
