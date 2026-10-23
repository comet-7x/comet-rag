from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx

from comet_rag.core.logging import logger
from comet_rag.infrastructure.models.reranker.qwen3_vl import AsyncQwen3VLReranker
from comet_rag.ports import AsyncEmbeddingPort, AsyncVectorSearchPort, SearchHit
from comet_rag.services.retrieval import (
    RetrievalService,
    RetrievalStage,
    SearchQuery,
)


async def test_invalid_provider_score_degrades_and_next_request_recovers() -> None:
    hits = [
        SearchHit("first", "first content", 0.8, {"source_id": "source-one"}),
        SearchHit("second", "second content", 0.6, {"source_id": "source-two"}),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            # 合法 JSON 数字在解码为 float 后溢出；必须经过真实适配器和基类。
            return httpx.Response(
                200,
                content=(
                    b'{"results":[{"index":0,"relevance_score":1e999},'
                    b'{"index":1,"relevance_score":0.4}]}'
                ),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )

    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = AsyncQwen3VLReranker(
                base_url="https://model.invalid/v1",
                model_name="qwen-rerank",
                api_key="test",
                client=client,
            )
            service = RetrievalService(
                embedding_model=cast(
                    "AsyncEmbeddingPort",
                    SimpleNamespace(embed_query=AsyncMock(return_value=[1.0])),
                ),
                vector_store=cast(
                    "AsyncVectorSearchPort",
                    SimpleNamespace(search=AsyncMock(return_value=hits)),
                ),
                reranker=reranker,
            )
            query = SearchQuery(kb_id="kb", query="query", top_k=2)
            degraded = await service.search(query)
            recovered = await service.search(query)
    finally:
        logger.remove(sink)

    assert not degraded.reranked
    assert [(item.id, item.text, item.score) for item in degraded.chunks] == [
        ("first", "first content", 0.8),
        ("second", "second content", 0.6),
    ]
    assert [item.metadata for item in degraded.chunks] == [
        {"source_id": "source-one"},
        {"source_id": "source-two"},
    ]
    assert all(math.isfinite(item.score) for item in degraded.chunks)
    assert len(degraded.degradations) == 1
    assert degraded.degradations[0].stage is RetrievalStage.RERANKER
    assert degraded.degradations[0].reason == "request_failed"
    assert degraded.degradations[0].error_type == "ValueError"
    assert any("降级为原始召回结果" in message for message in messages)

    assert recovered.reranked
    assert [(item.id, item.score) for item in recovered.chunks] == [
        ("second", 0.9),
        ("first", 0.2),
    ]
    assert recovered.degradations == ()
    assert len(requests) == 2
    assert [json.loads(request.content)["documents"] for request in requests] == [
        ["first content", "second content"],
        ["first content", "second content"],
    ]
