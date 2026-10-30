from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from comet_rag.vectorstores.types import VectorHit

from .fusion import reciprocal_rank_fusion
from .types import RetrievalEvidence


def group_and_fuse(
    channels: Mapping[str, Sequence[VectorHit]],
) -> tuple[tuple[str, ...], dict[str, tuple[RetrievalEvidence, ...]], dict[str, float]]:
    ranks: dict[str, list[str]] = {}
    evidence: dict[str, list[RetrievalEvidence]] = {}
    for channel, hits in channels.items():
        seen: set[str] = set()
        ordered = []
        previous: VectorHit | None = None
        for rank, hit in enumerate(hits, 1):
            if not math.isfinite(hit.score):
                raise ValueError("retrieval score must be finite")
            if previous is not None:
                if (hit.metric, hit.higher_is_better) != (
                    previous.metric,
                    previous.higher_is_better,
                ):
                    raise ValueError("mixed score semantics in one channel")
                if hit.higher_is_better:
                    if hit.score > previous.score:
                        raise ValueError("backend hits must be in relevant order")
                else:
                    if hit.score < previous.score:
                        raise ValueError("backend hits must be in relevant order")
            previous = hit
            evidence.setdefault(hit.unit_id, []).append(
                RetrievalEvidence(channel, rank, hit)
            )
            if hit.unit_id not in seen:
                seen.add(hit.unit_id)
                ordered.append(hit.unit_id)
        ranks[channel] = ordered
    scores: dict[str, float] = {}
    if len(ranks) == 1:
        order = next(iter(ranks.values()))
    else:
        fused = reciprocal_rank_fusion(ranks)
        order = [item.unit_id for item in fused]
        scores = {item.unit_id: item.score for item in fused}
    return tuple(order), {key: tuple(value) for key, value in evidence.items()}, scores
