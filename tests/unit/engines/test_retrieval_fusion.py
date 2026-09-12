from __future__ import annotations

import subprocess
import sys
from copy import deepcopy

import pytest

from comet_rag.engines.retrieval import (
    DEFAULT_RRF_K,
    RankContribution,
    reciprocal_rank_fusion,
)
from comet_rag.ports import SearchHit


def hit(rid: str, score: float, *, text: str | None = None) -> SearchHit:
    return SearchHit(
        id=rid,
        text=text or f"text-{rid}",
        score=score,
        metadata={"source": f"{rid}.md"},
    )


def test_rrf_uses_one_based_rank_and_sums_channel_contributions() -> None:
    fused = reciprocal_rank_fusion(
        {
            "dense": [hit("a", 0.9), hit("shared", 0.8)],
            "keyword": [hit("shared", 12.0), hit("b", 8.0)],
        }
    )

    assert [item.id for item in fused] == ["shared", "a", "b"]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0].contributions == {
        "dense": RankContribution(rank=2, score=0.8),
        "keyword": RankContribution(rank=1, score=12.0),
    }


def test_rrf_does_not_compare_or_add_raw_channel_scores() -> None:
    fused = reciprocal_rank_fusion(
        {
            "dense": [hit("dense-first", -1000.0)],
            "keyword": [hit("keyword-first", 1_000_000.0)],
        }
    )

    assert [item.id for item in fused] == ["dense-first", "keyword-first"]
    assert fused[0].score == pytest.approx(1 / (DEFAULT_RRF_K + 1))
    assert fused[1].score == fused[0].score


def test_rrf_deduplicates_repeated_ids_within_and_across_channels() -> None:
    fused = reciprocal_rank_fusion(
        {
            "dense": [hit("same", 0.9), hit("same", 0.8)],
            "keyword": [hit("same", 9.0)],
        }
    )

    assert len(fused) == 1
    assert fused[0].score == pytest.approx(2 / (DEFAULT_RRF_K + 1))
    assert fused[0].contributions["dense"] == RankContribution(
        rank=1, score=0.9
    )


def test_rrf_accepts_a_missing_or_empty_channel() -> None:
    dense = [hit("a", 0.9), hit("b", 0.8)]

    dense_only = reciprocal_rank_fusion({"dense": dense})
    with_empty_keyword = reciprocal_rank_fusion(
        {"dense": dense, "keyword": []}
    )

    assert dense_only == with_empty_keyword
    assert [item.id for item in dense_only] == ["a", "b"]
    assert reciprocal_rank_fusion({}) == []


def test_equal_fusion_scores_are_ordered_by_id() -> None:
    fused = reciprocal_rank_fusion(
        {
            "keyword": [hit("b", 99.0)],
            "dense": [hit("a", -1.0)],
        }
    )

    assert [item.id for item in fused] == ["a", "b"]


def test_rrf_does_not_mutate_inputs_or_share_metadata() -> None:
    channels = {
        "dense": [hit("a", 0.9)],
        "keyword": [hit("a", 9.0), hit("b", 8.0)],
    }
    before = deepcopy(channels)

    fused = reciprocal_rank_fusion(channels)
    fused[0].metadata["new"] = True

    assert channels == before
    assert all("new" not in item.metadata for hits in channels.values() for item in hits)


@pytest.mark.parametrize("rrf_k", [0, -1, True, 1.5])
def test_invalid_rrf_k_is_rejected(rrf_k: object) -> None:
    with pytest.raises(ValueError, match="rrf_k"):
        reciprocal_rank_fusion({}, rrf_k=rrf_k)  # type: ignore[arg-type]


def test_blank_channel_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="通道名称"):
        reciprocal_rank_fusion({" ": [hit("a", 1.0)]})


def test_importing_rrf_does_not_load_milvus() -> None:
    code = (
        "import sys; import comet_rag.engines.retrieval; "
        "assert 'pymilvus' not in sys.modules"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
