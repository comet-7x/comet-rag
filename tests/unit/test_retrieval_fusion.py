from __future__ import annotations

from copy import deepcopy

import pytest

from comet_rag.retrieval import reciprocal_rank_fusion


def test_one_based_ranks_and_channel_contributions() -> None:
    fused = reciprocal_rank_fusion(
        {"dense": ["a", "shared"], "keyword": ["shared", "b"]}
    )
    assert [item.unit_id for item in fused] == ["shared", "a", "b"]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0].ranks == {"dense": 2, "keyword": 1}


def test_duplicates_do_not_inflate_scores_or_leave_rank_gaps() -> None:
    fused = reciprocal_rank_fusion({"dense": ["a", "a", "b"], "keyword": ["b", "b"]})
    assert [item.unit_id for item in fused] == ["b", "a"]
    assert fused[0].ranks == {"dense": 2, "keyword": 1}
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1].score == pytest.approx(1 / 61)


def test_equal_scores_use_unit_id_order() -> None:
    left = reciprocal_rank_fusion({"keyword": ["b"], "dense": ["a"]})
    right = reciprocal_rank_fusion({"dense": ["a"], "keyword": ["b"]})
    assert left == right
    assert [item.unit_id for item in left] == ["a", "b"]
    assert left[0].score == pytest.approx(1 / 61)


def test_empty_channels_and_inputs() -> None:
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"dense": []}) == []
    assert reciprocal_rank_fusion(
        {"dense": ["a"], "keyword": []}
    ) == reciprocal_rank_fusion({"dense": ["a"]})


def test_results_do_not_mutate_or_share_input_state() -> None:
    channels = {"dense": ["a"], "keyword": ["a", "b"]}
    before = deepcopy(channels)
    first = reciprocal_rank_fusion(channels)
    first[0].ranks["dense"] = 999
    assert channels == before
    assert reciprocal_rank_fusion(channels)[0].ranks == {"dense": 1, "keyword": 1}


@pytest.mark.parametrize("rrf_k", [0, -1, True, 1.5])
def test_invalid_smoothing_is_rejected(rrf_k: object) -> None:
    with pytest.raises(ValueError, match="rrf_k"):
        reciprocal_rank_fusion({}, rrf_k=rrf_k)  # type: ignore[arg-type]


@pytest.mark.parametrize("channel", ["", " "])
def test_blank_channel_names_are_rejected(channel: str) -> None:
    with pytest.raises(ValueError, match="channel name"):
        reciprocal_rank_fusion({channel: []})


@pytest.mark.parametrize("unit_id", ["", " "])
def test_blank_unit_ids_are_rejected(unit_id: str) -> None:
    with pytest.raises(ValueError, match="unit_id"):
        reciprocal_rank_fusion({"dense": [unit_id]})
