from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Literal, cast

import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.models.reranker.base import (
    AsyncBaseReranker,
    BaseReranker,
)
from comet_rag.ports import RankedDocument


class Scores(BaseReranker[str]):
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.failure: BaseException | None = None

    # Any 对齐模板允许的供应商扩展参数，替身不消费这些参数。
    def _score(
        self, query: str, documents: Sequence[str], **kwargs: Any
    ) -> list[float]:
        if self.failure is not None:
            raise self.failure
        return self.values


class AsyncScores(AsyncBaseReranker[str]):
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.failure: BaseException | None = None

    # Any 对齐模板允许的供应商扩展参数，替身不消费这些参数。
    async def _score(
        self, query: str, documents: Sequence[str], **kwargs: Any
    ) -> list[float]:
        if self.failure is not None:
            raise self.failure
        return self.values


async def invoke(
    model: Scores | AsyncScores, method: Literal["score", "rank"]
) -> list[float] | list[RankedDocument]:
    if isinstance(model, AsyncScores):
        if method == "score":
            return await model.score("query", ["first", "second"])
        return await model.rank("query", ["first", "second"], top_k=1)
    if method == "score":
        return model.score("query", ["first", "second"])
    return model.rank("query", ["first", "second"], top_k=1)


@pytest.mark.parametrize("model_type", [Scores, AsyncScores], ids=["sync", "async"])
@pytest.mark.parametrize("method", ["score", "rank"])
@pytest.mark.parametrize(
    "bad_score",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="negative-inf"),
        pytest.param(True, id="bool"),
        pytest.param("0.5", id="numeric-string"),
        pytest.param("sensitive-score", id="sensitive-string"),
        pytest.param(None, id="none"),
        pytest.param(10**400, id="overflow"),
    ],
)
async def test_all_scores_are_validated_before_return_or_top_k(
    model_type: type[Scores | AsyncScores],
    method: Literal["score", "rank"],
    bad_score: object,
) -> None:
    # 供应商实现可能违反注解；第二个候选也必须检查，不能只检查截断后的第一条。
    model = model_type([0.8, cast("float", bad_score)])

    with pytest.raises(ValueError, match=r"分数\[1\].*有限") as error:
        await invoke(model, method)

    assert "sensitive-score" not in str(error.value)


@pytest.mark.parametrize("model_type", [Scores, AsyncScores], ids=["sync", "async"])
async def test_finite_scores_keep_values_and_empty_batches(
    model_type: type[Scores | AsyncScores],
) -> None:
    model = model_type([-2.5, 3])
    assert await invoke(model, "score") == [-2.5, 3]
    ranked = await invoke(model, "rank")
    assert len(ranked) == 1
    assert isinstance(ranked[0], RankedDocument)
    assert (ranked[0].index, ranked[0].score) == (1, 3)

    model.values = []
    if isinstance(model, AsyncScores):
        assert await model.score("query", []) == []
        assert await model.rank("query", []) == []
    else:
        assert model.score("query", []) == []
        assert model.rank("query", []) == []


@pytest.mark.parametrize("model_type", [Scores, AsyncScores], ids=["sync", "async"])
async def test_invalid_scores_release_gate_and_allow_recovery(
    model_type: type[Scores | AsyncScores],
) -> None:
    gate = Gate(limit=1, max_waiting=0)
    model = model_type([0.8, float("nan")])
    model.bind_gate(gate)

    with pytest.raises(ValueError):
        await invoke(model, "score")

    assert gate.stats.in_flight == 0
    model.values = [0.8, 0.6]
    assert await invoke(model, "score") == [0.8, 0.6]
    assert gate.stats.in_flight == 0


@pytest.mark.parametrize("model_type", [Scores, AsyncScores], ids=["sync", "async"])
@pytest.mark.parametrize("failure_type", [ConnectionError, asyncio.CancelledError])
async def test_provider_errors_and_cancellation_propagate_without_gate_leaks(
    model_type: type[Scores | AsyncScores], failure_type: type[BaseException]
) -> None:
    gate = Gate(limit=1, max_waiting=0)
    model = model_type([0.8, 0.6])
    model.bind_gate(gate)
    failure = failure_type("injected failure")
    model.failure = failure

    with pytest.raises(failure_type) as error:
        await invoke(model, "score")

    assert error.value is failure
    assert gate.stats.in_flight == 0
    model.failure = None
    assert await invoke(model, "score") == [0.8, 0.6]
