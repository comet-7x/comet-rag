"""检索 HTTP DTO 与用例参数之间的兼容边界。"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from comet_rag.api.main import _install_exception_handlers
from comet_rag.api.routes.search import search
from comet_rag.api.schemas.search import SearchRequest
from comet_rag.services.retrieval import (
    HybridRecallFailed,
    KeywordSearchUnavailable,
    RecallChannel,
    RetrievalDegradation,
    RetrievalResult,
    RetrievalService,
    RetrievalStage,
    RetrievedChunk,
    SearchMode,
    SearchQuery,
)


class _RecordingRetrieval:
    def __init__(self) -> None:
        self.query: SearchQuery | None = None

    async def search(self, query: SearchQuery) -> RetrievalResult:
        self.query = query
        return RetrievalResult(
            chunks=[
                RetrievedChunk(
                    id="shared",
                    text="hybrid result",
                    score=0.75,
                    metadata={"source": "doc.md"},
                    vector_score=0.9,
                    keyword_score=12.0,
                    fusion_score=0.03,
                    vector_rank=2,
                    keyword_rank=1,
                )
            ],
            reranked=True,
            fetched=3,
            effective_top_k=1,
            mode=SearchMode.HYBRID,
            channels=(RecallChannel.DENSE, RecallChannel.KEYWORD),
            degradations=(
                RetrievalDegradation(
                    stage=RetrievalStage.RERANKER,
                    reason="request_failed",
                    error_type="TimeoutError",
                ),
            ),
        )


def test_search_request_defaults_to_dense_for_backward_compatibility() -> None:
    request = SearchRequest(kb_id="kb", query="query")

    assert request.mode is SearchMode.DENSE


def test_search_request_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(
            {"kb_id": "kb", "query": "query", "mode": "semantic"}
        )


async def test_search_route_forwards_mode_and_returns_diagnostics() -> None:
    retrieval = _RecordingRetrieval()

    response = await search(
        SearchRequest(
            kb_id="kb",
            query="query",
            top_k=1,
            fetch_k=3,
            mode=SearchMode.HYBRID,
        ),
        cast("RetrievalService", retrieval),
    )
    payload = response.model_dump(mode="json")

    assert retrieval.query is not None
    assert retrieval.query.mode is SearchMode.HYBRID
    assert payload["mode"] == "hybrid"
    assert payload["channels"] == ["dense", "keyword"]
    assert payload["degradations"] == [
        {
            "stage": "reranker",
            "reason": "request_failed",
            "error_type": "TimeoutError",
        }
    ]
    assert payload["chunks"][0] == {
        "id": "shared",
        "text": "hybrid result",
        "score": 0.75,
        "metadata": {"source": "doc.md"},
        "vector_score": 0.9,
        "keyword_score": 12.0,
        "fusion_score": 0.03,
        "vector_rank": 2,
        "keyword_rank": 1,
    }


def test_all_hybrid_channels_failed_maps_to_safe_503() -> None:
    app = FastAPI()
    _install_exception_handlers(app)

    @app.get("/failure")
    async def failure() -> None:
        raise HybridRecallFailed(
            (
                RetrievalDegradation(
                    stage=RetrievalStage.DENSE,
                    reason="channel_unavailable",
                    error_type="TimeoutError",
                ),
                RetrievalDegradation(
                    stage=RetrievalStage.KEYWORD,
                    reason="channel_unavailable",
                    error_type="ConnectionError",
                ),
            )
        )

    response = TestClient(app).get("/failure")

    assert response.status_code == 503
    assert response.json()["error"] == (
        "hybrid 召回的所有通道均失败（dense:TimeoutError, keyword:ConnectionError）"
    )


def test_missing_keyword_capability_maps_to_503() -> None:
    app = FastAPI()
    _install_exception_handlers(app)

    @app.get("/failure")
    async def failure() -> None:
        raise KeywordSearchUnavailable("keyword search is not configured")

    response = TestClient(app).get("/failure")

    assert response.status_code == 503
