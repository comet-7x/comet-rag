from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from comet_rag.api.deps import get_retrieval
from comet_rag.api.routes.search import router
from comet_rag.core.logging import logger
from comet_rag.ports import (
    AsyncEmbeddingPort,
    AsyncRerankerPort,
    AsyncVectorSearchPort,
    RankedDocument,
    RerankDocument,
    SearchHit,
)
from comet_rag.services.retrieval import (
    RetrievalService,
    RetrievalStage,
    RetrievedChunk,
    SearchQuery,
)


def ranked(index: object, score: object = 1.0) -> RankedDocument:
    # 故意绕过静态类型：真实 Port 实现也可能返回未经过 AsyncBaseReranker 校验的数据。
    return RankedDocument(
        index=cast("int", index),
        score=cast("float", score),
        document=RerankDocument(id="untrusted", content="untrusted content"),
    )


@pytest.fixture
def hits() -> list[SearchHit]:
    return [
        SearchHit("first", "first content", 0.8, {"nested": {"value": "original"}}),
        SearchHit("second", "second content", 0.6, {"source_id": "source"}),
    ]


def service(hits: list[SearchHit], rank: AsyncMock) -> RetrievalService:
    # 用原始协议替身，确保坏响应确实到达服务边界，而非被适配器基类提前拦住。
    return RetrievalService(
        embedding_model=cast(
            "AsyncEmbeddingPort",
            SimpleNamespace(embed_query=AsyncMock(return_value=[1.0])),
        ),
        vector_store=cast(
            "AsyncVectorSearchPort",
            SimpleNamespace(search=AsyncMock(return_value=hits)),
        ),
        reranker=cast("AsyncRerankerPort", SimpleNamespace(rank=rank)),
    )


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        pytest.param(None, "invalid_result", id="none"),
        pytest.param(object(), "invalid_result", id="non-list"),
        pytest.param([object(), object()], "invalid_result", id="invalid-items"),
        pytest.param([], "result_count_mismatch", id="missing-results"),
        pytest.param([ranked(0), ranked(0)], "result_index_mismatch", id="duplicate"),
        pytest.param([ranked(-1), ranked(1)], "result_index_mismatch", id="negative"),
        pytest.param(
            [ranked(0), ranked(2)], "result_index_mismatch", id="out-of-range"
        ),
        pytest.param([ranked(0.0), ranked(1)], "result_index_mismatch", id="float"),
        pytest.param([ranked(False), ranked(1)], "result_index_mismatch", id="bool"),
        pytest.param([ranked("0"), ranked(1)], "result_index_mismatch", id="string"),
    ],
)
async def test_malformed_port_results_fall_back_without_changing_recall(
    hits: list[SearchHit], response: object, reason: str
) -> None:
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        result = await service(hits, AsyncMock(return_value=response)).search(
            SearchQuery(kb_id="kb", query="query", top_k=2)
        )
    finally:
        logger.remove(sink)

    assert not result.reranked
    assert [(item.id, item.text, item.score) for item in result.chunks] == [
        ("first", "first content", 0.8),
        ("second", "second content", 0.6),
    ]
    assert result.degradations[0].stage is RetrievalStage.RERANKER
    assert result.degradations[0].reason == reason
    assert result.degradations[0].error_type is None
    assert any("降级为原始召回结果" in message for message in messages)


@pytest.mark.parametrize(
    "score",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(True, id="bool"),
        pytest.param("0.5", id="string"),
        pytest.param(10**400, id="overflow"),
    ],
)
async def test_invalid_scores_fall_back_to_finite_recall_scores(
    hits: list[SearchHit], score: object
) -> None:
    result = await service(
        hits, AsyncMock(return_value=[ranked(0, score), ranked(1, 0.5)])
    ).search(SearchQuery(kb_id="kb", query="query", top_k=2))

    assert not result.reranked
    assert [item.score for item in result.chunks] == [0.8, 0.6]
    assert result.degradations[0].reason == "result_score_invalid"


async def test_reranker_cannot_corrupt_fallback_metadata(hits: list[SearchHit]) -> None:
    original = deepcopy(hits)

    async def mutate_then_fail(query, documents):
        documents[0].metadata["extra"] = "corrupted"
        documents[0].metadata["nested"]["value"] = "corrupted"
        raise TimeoutError("private upstream detail")

    result = await service(hits, AsyncMock(side_effect=mutate_then_fail)).search(
        SearchQuery(kb_id="kb", query="query", top_k=2)
    )

    assert not result.reranked
    assert result.chunks[0].metadata == {"nested": {"value": "original"}}
    assert hits == original
    assert result.degradations[0].reason == "request_failed"
    assert result.degradations[0].error_type == "TimeoutError"
    assert "private upstream detail" not in str(result.degradations)


async def test_reranker_cancellation_still_propagates(hits: list[SearchHit]) -> None:
    error = asyncio.CancelledError("request cancelled")
    with pytest.raises(asyncio.CancelledError) as caught:
        await service(hits, AsyncMock(side_effect=error)).search(
            SearchQuery(kb_id="kb", query="query")
        )
    assert caught.value is error


async def test_successful_rerank_preserves_candidate_fields_and_original_scores() -> (
    None
):
    candidates = [
        RetrievedChunk(
            id="first",
            text="original content",
            score=0.8,
            metadata={"source_id": "source"},
            vector_score=0.8,
            keyword_score=1.2,
            fusion_score=0.03,
            vector_rank=1,
            keyword_rank=2,
            matched_child_id="child",
        )
    ]
    rank = AsyncMock(return_value=[ranked(0, 0.9)])
    chunks, reranked, degradation = await RetrievalService._rerank(  # noqa: SLF001
        cast("AsyncRerankerPort", SimpleNamespace(rank=rank)), "query", candidates
    )

    assert reranked
    assert degradation is None
    assert chunks[0].to_dict() == {
        "id": "first",
        "text": "original content",
        "score": 0.9,
        "metadata": {"source_id": "source"},
        "vector_score": 0.8,
        "keyword_score": 1.2,
        "fusion_score": 0.03,
        "vector_rank": 1,
        "keyword_rank": 2,
        "matched_child_id": "child",
    }
    assert candidates[0].score == 0.8


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
async def test_search_http_never_serializes_invalid_rerank_score_as_null(
    hits: list[SearchHit], score: float
) -> None:
    retrieval = service(hits, AsyncMock(return_value=[ranked(0, score), ranked(1)]))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_retrieval] = lambda: retrieval

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/search", json={"kb_id": "kb", "query": "q"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["reranked"] is False
    assert [item["score"] for item in payload["chunks"]] == [0.8, 0.6]
    assert payload["degradations"][0]["reason"] == "result_score_invalid"
