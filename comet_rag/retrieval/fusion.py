from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FusedRank:
    unit_id: str
    score: float
    ranks: dict[str, int]


def reciprocal_rank_fusion(
    channels: Mapping[str, Sequence[str]],
    *,
    rrf_k: int = 60,
) -> list[FusedRank]:
    """融合各通道的单元排名；通道内重复 ID 仅贡献一次且不占用后续名次。"""
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
        raise ValueError("rrf_k must be a positive integer")

    ranks_by_unit: dict[str, dict[str, int]] = {}
    for channel, unit_ids in channels.items():
        if not isinstance(channel, str) or not channel.strip():
            raise ValueError("channel name must be a nonblank string")
        seen: set[str] = set()
        for unit_id in unit_ids:
            if not isinstance(unit_id, str) or not unit_id.strip():
                raise ValueError("unit_id must be a nonblank string")
            if unit_id in seen:
                continue
            seen.add(unit_id)
            ranks_by_unit.setdefault(unit_id, {})[channel] = len(seen)

    fused = [
        FusedRank(
            unit_id=unit_id,
            score=sum(1 / (rrf_k + rank) for _, rank in sorted(ranks.items())),
            ranks=dict(ranks),
        )
        for unit_id, ranks in ranks_by_unit.items()
    ]
    return sorted(fused, key=lambda item: (-item.score, item.unit_id))
