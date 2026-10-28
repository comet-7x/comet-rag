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
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id must be nonblank")
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("rank must be a positive one-based integer")
        if self.score is not None:
            if (
                isinstance(self.score, bool)
                or not isinstance(self.score, (int, float))
                or not math.isfinite(self.score)
            ):
                raise ValueError("score must be finite or absent")
            object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True, slots=True)
class RerankResult:
    items: tuple[RankedItem, ...]
    model: str
    score_kind: str
    revision: str | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.model, self.score_kind)
        ):
            raise ValueError("model and score_kind must be nonblank")
        if self.revision is not None and (
            not isinstance(self.revision, str) or not self.revision.strip()
        ):
            raise ValueError("revision must be nonblank or unknown")
        items = tuple(self.items)
        if any(not isinstance(item, RankedItem) for item in items):
            raise ValueError("items must be ranked candidates")
        if len({item.candidate_id for item in items}) != len(items) or tuple(
            item.rank for item in items
        ) != tuple(range(1, len(items) + 1)):
            raise ValueError(
                "ranked candidate IDs must be unique with consecutive one-based ranks"
            )
        object.__setattr__(self, "items", items)

    def validate(self, candidates: Sequence[ContentItem]) -> None:
        """完整结果必须恰好覆盖本次候选；调用方仍负责选择评分内容。"""
        items = _candidates(candidates)
        if {item.candidate_id for item in self.items} != {item.id for item in items}:
            raise ValueError("rerank result must cover every candidate exactly once")


class AsyncReranker(Protocol):
    async def rerank(
        self, query: ContentInput, candidates: Sequence[ContentItem]
    ) -> RerankResult: ...


def _candidates(candidates: Sequence[ContentItem]) -> tuple[ContentItem, ...]:
    items = tuple(candidates)
    if any(not isinstance(item, ContentItem) for item in items) or len(
        {item.id for item in items}
    ) != len(items):
        raise ValueError("candidates must be ContentItems with unique IDs")
    return items
