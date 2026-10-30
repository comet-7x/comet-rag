from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from comet_rag.content import ContentInput, ContentItem


@dataclass(frozen=True, slots=True)
class RankedItem:
    candidate_id: str
    rank: int
    score: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str):
            raise ValueError("candidate_id must be a string")
        if not self.candidate_id.strip():
            raise ValueError("candidate_id must be nonblank")
        if type(self.rank) is not int:
            raise ValueError("rank must be a positive one-based integer")
        if self.rank < 1:
            raise ValueError("rank must be a positive one-based integer")
        if self.score is not None:
            if isinstance(self.score, bool):
                raise ValueError("score must be finite or absent")
            if not isinstance(self.score, (int, float)):
                raise ValueError("score must be finite or absent")
            if not math.isfinite(self.score):
                raise ValueError("score must be finite or absent")
            object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True, slots=True)
class RerankResult:
    items: tuple[RankedItem, ...]
    model: str
    score_kind: str
    revision: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("model", self.model),
            ("score_kind", self.score_kind),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if not value.strip():
                raise ValueError(f"{name} must be nonblank")
        if self.revision is not None:
            if not isinstance(self.revision, str):
                raise ValueError("revision must be a string")
            if not self.revision.strip():
                raise ValueError("revision must be nonblank or unknown")
        items = tuple(self.items)
        if any(not isinstance(item, RankedItem) for item in items):
            raise ValueError("items must be ranked candidates")
        if len({item.candidate_id for item in items}) != len(items):
            raise ValueError(
                "ranked candidate IDs must be unique with consecutive one-based ranks"
            )
        if tuple(item.rank for item in items) != tuple(range(1, len(items) + 1)):
            raise ValueError(
                "ranked candidate IDs must be unique with consecutive one-based ranks"
            )
        object.__setattr__(self, "items", items)

    def validate(self, candidates: Sequence[ContentItem]) -> None:
        """完整结果必须恰好覆盖本次候选；调用方仍负责选择评分内容。"""
        items = _validate_candidates(candidates)
        if {item.candidate_id for item in self.items} != {item.id for item in items}:
            raise ValueError("rerank result must cover every candidate exactly once")


class AsyncReranker(Protocol):
    async def rerank(
        self, query: ContentInput, candidates: Sequence[ContentItem]
    ) -> RerankResult: ...


def _validate_candidates(candidates: Sequence[ContentItem]) -> tuple[ContentItem, ...]:
    items = tuple(candidates)
    if any(not isinstance(item, ContentItem) for item in items):
        raise ValueError("candidates must be ContentItems with unique IDs")
    if len({item.id for item in items}) != len(items):
        raise ValueError("candidates must be ContentItems with unique IDs")
    return items
