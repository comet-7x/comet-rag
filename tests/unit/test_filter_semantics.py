from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from comet_rag.api.schemas.search import SearchRequest
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.infrastructure.persistence.vector_store.milvus import (
    AsyncMilvusStore,
    compile_filter,
)
from comet_rag.ports import Filter
from comet_rag.ports.filtering import (
    MAX_FILTER_NUMBER,
    matches_filter,
    normalize_filter,
)
from comet_rag.services.retrieval import SearchQuery


@pytest.mark.parametrize(
    "value",
    [
        None,
        [None],
        {"x": 1},
        [True, 1],
        [1, 1.0],
        ["1", 1],
        float("nan"),
        float("inf"),
        float("-inf"),
        2**53,
        float(2**53),
        -(2**53),
        object(),
    ],
)
def test_invalid_values_are_rejected_by_all_boundaries(value: object) -> None:
    condition = {"v": value}
    with pytest.raises(ValueError):
        normalize_filter(condition)
    with pytest.raises(ValueError):
        compile_filter(condition)
    for model in (SearchRequest, SearchQuery):
        with pytest.raises(ValidationError):
            model(kb_id="kb", query="q", filter=cast("Filter", condition))


@pytest.mark.parametrize("condition", [{"": 1}, {1: 1}, [], "bad"])
def test_invalid_filter_structure_is_rejected(condition: object) -> None:
    with pytest.raises(ValueError):
        normalize_filter(condition)


@pytest.mark.parametrize("values", [[1, 1], (1, 1), {1}])
def test_collections_are_membership_candidates_not_array_equality(values) -> None:
    condition = normalize_filter({"v": values})
    assert matches_filter({"v": 1.0}, condition)
    assert not matches_filter({"v": True}, condition)
    assert not matches_filter({"v": [1]}, condition)
    assert not matches_filter({"v": None}, condition)
    assert not matches_filter({}, condition)


def test_normalization_copies_collections_and_preserves_scalar_types() -> None:
    values = [True]
    condition = normalize_filter({"v": values, "n": 1, "f": 1.0})
    values.append(False)
    assert condition == {"v": [True], "n": 1, "f": 1.0}
    for model in (SearchRequest, SearchQuery):
        result = model(kb_id="kb", query="q", filter=condition).filter
        assert result is not None
        assert type(result["n"]) is int and type(result["f"]) is float
        candidates = result["v"]
        assert isinstance(candidates, list)
        assert type(candidates[0]) is bool


@pytest.mark.parametrize("number", [-MAX_FILTER_NUMBER, MAX_FILTER_NUMBER, 1.5, -0.0])
def test_safe_numeric_boundaries_are_preserved(number: float) -> None:
    assert normalize_filter({"n": number}) == {"n": number}


@pytest.mark.parametrize("operation", ["dense", "keyword", "count", "delete"])
async def test_empty_store_still_validates_the_whole_filter(operation: str) -> None:
    store = AsyncInMemoryVectorStore()
    await store.ensure_collection("kb", dim=2)
    condition = cast("Filter", {"absent": "no-match", "bad": None})
    with pytest.raises(ValueError):
        if operation == "dense":
            await store.search("kb", [1.0, 0.0], filter=condition)
        elif operation == "keyword":
            await store.search_keywords("kb", " ", filter=condition)
        elif operation == "count":
            await store.count("kb", filter=condition)
        else:
            await store.delete("kb", ids=[], filter=condition)


async def test_filter_is_snapshotted_before_milvus_discovery_waits() -> None:
    target = AsyncMilvusStore.__new__(AsyncMilvusStore)
    client = AsyncMock()
    client.query.return_value = [{"count(*)": 1}]
    target._client = client
    target._prefix = "snapshot"
    target._consistency = "Strong"
    entered, release = asyncio.Event(), asyncio.Event()

    async def dimension(kb_id: str) -> int:
        entered.set()
        await release.wait()
        return 2

    target._dim_of = dimension
    candidates = ["original"]
    condition = normalize_filter({"v": candidates})
    task = asyncio.create_task(target.count("kb", filter=condition))
    await entered.wait()
    # 模拟在首次 I/O 暂停期间污染库调用方持有的已验证条件。
    stored_candidates = condition["v"]
    assert isinstance(stored_candidates, list)
    stored_candidates.append("changed")
    release.set()
    assert await task == 1
    assert client.query.call_args.kwargs["filter_params"] == {"f0": ["original"]}
