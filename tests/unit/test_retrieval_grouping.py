from __future__ import annotations

from dataclasses import replace

import pytest

from comet_rag.retrieval.grouping import group_and_fuse
from comet_rag.vectorstores.types import VectorHit


def hit(unit: str, rep: str, score: float) -> VectorHit:
    return VectorHit(
        f"record-{rep}",
        rep,
        unit,
        "source",
        "document",
        "scope",
        "profile",
        "space",
        score,
        "COSINE",
        True,
    )


def test_best_representation_gives_continuous_unit_rank_and_keeps_all_evidence() -> (
    None
):
    dense = (hit("a", "a1", 0.9), hit("a", "a2", 0.8), hit("b", "b1", 0.7))
    keyword = (replace(hit("b", "b2", 8.0), metric="BM25"),)
    order, evidence, scores = group_and_fuse({"dense": dense, "keyword": keyword})
    assert order == ("b", "a")
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61)
    assert [(e.channel, e.rank, e.hit.representation_id) for e in evidence["a"]] == [
        ("dense", 1, "a1"),
        ("dense", 2, "a2"),
    ]
    assert [(e.channel, e.rank) for e in evidence["b"]] == [
        ("dense", 3),
        ("keyword", 1),
    ]


def test_single_channel_preserves_native_scores_and_distance_direction() -> None:
    hits = tuple(
        replace(hit(unit, unit, score), metric="L2", higher_is_better=False)
        for unit, score in (("b", 0.1), ("a", 0.8))
    )
    order, evidence, scores = group_and_fuse({"dense": hits})
    assert order == ("b", "a") and scores == {}
    assert evidence["b"][0].hit.score == 0.1
    assert group_and_fuse({"dense": ()}) == ((), {}, {})


@pytest.mark.parametrize(
    "second",
    [
        hit("b", "b", 0.95),
        replace(hit("b", "b", 0.8), metric="IP"),
        replace(hit("b", "b", 0.8), higher_is_better=False),
        hit("b", "b", float("nan")),
    ],
)
def test_invalid_backend_score_semantics_fail(second: VectorHit) -> None:
    with pytest.raises(ValueError):
        group_and_fuse({"dense": (hit("a", "a", 0.9), second)})
