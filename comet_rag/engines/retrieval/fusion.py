"""与检索后端无关的候选融合策略。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from comet_rag.ports import SearchHit

DEFAULT_RRF_K = 60


@dataclass(frozen=True, slots=True)
class RankContribution:
    """一个召回通道对融合结果的原始贡献。"""

    rank: int
    score: float


@dataclass(slots=True)
class FusedHit:
    id: str
    text: str
    score: float
    metadata: dict[str, object] = field(default_factory=dict)
    contributions: dict[str, RankContribution] = field(default_factory=dict)


def reciprocal_rank_fusion(
    channels: Mapping[str, Sequence[SearchHit]],
    *,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[FusedHit]:
    """按排名融合多个召回通道，不比较不可互换的原始分数。"""
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
        raise ValueError(f"rrf_k 必须为正整数，收到 {rrf_k!r}")

    representatives: dict[str, SearchHit] = {}
    scores: dict[str, float] = {}
    contributions: dict[str, dict[str, RankContribution]] = {}

    for channel, hits in channels.items():
        if not channel.strip():
            raise ValueError("召回通道名称不能为空")
        seen: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            # 同一通道重复返回一个 chunk 不应凭重复次数抬高融合分数。
            if hit.id in seen:
                continue
            seen.add(hit.id)
            representatives.setdefault(hit.id, hit)
            scores[hit.id] = scores.get(hit.id, 0.0) + 1 / (rrf_k + rank)
            contributions.setdefault(hit.id, {})[channel] = RankContribution(
                rank=rank,
                score=hit.score,
            )

    fused = [
        FusedHit(
            id=hit_id,
            text=representatives[hit_id].text,
            score=score,
            metadata=dict(representatives[hit_id].metadata),
            contributions=dict(contributions[hit_id]),
        )
        for hit_id, score in scores.items()
    ]
    fused.sort(key=lambda hit: (-hit.score, hit.id))
    return fused


__all__ = [
    "DEFAULT_RRF_K",
    "FusedHit",
    "RankContribution",
    "reciprocal_rank_fusion",
]
