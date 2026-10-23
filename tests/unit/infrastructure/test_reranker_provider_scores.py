from __future__ import annotations

import json
import math
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Literal, cast

import httpx
import pytest

from comet_rag.infrastructure.models.reranker.qwen3_vl import (
    AsyncQwen3VLReranker,
    ChatCompletionContentPartTextParam,
    Qwen3VLReranker,
    ScoreMultiModalParam,
)


@asynccontextmanager
async def _provider(
    async_mode: bool, response_body: str, requests: list[httpx.Request]
) -> AsyncIterator[Qwen3VLReranker | AsyncQwen3VLReranker]:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # raw JSON 才能保留 1e999；httpx 的 json= 会预先拒绝 Python inf。
        return httpx.Response(
            200, content=response_body, headers={"Content-Type": "application/json"}
        )

    transport = httpx.MockTransport(respond)
    if async_mode:
        async with httpx.AsyncClient(transport=transport) as client:
            yield AsyncQwen3VLReranker(
                base_url="https://model.invalid/v1",
                model_name="qwen-rerank",
                api_key="test",
                client=client,
            )
    else:
        with httpx.Client(transport=transport) as client:
            yield Qwen3VLReranker(
                base_url="https://model.invalid/v1",
                model_name="qwen-rerank",
                api_key="test",
                client=client,
            )


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method", ["score", "rank"])
@pytest.mark.parametrize(
    "invalid_json", ["1e999", "-1e999", '"NaN"'], ids=["inf", "-inf", "nan"]
)
async def test_qwen_non_finite_response_is_rejected_before_return_or_truncation(
    async_mode: bool, method: Literal["score", "rank"], invalid_json: str
) -> None:
    response = (
        '{"results":[{"index":0,"relevance_score":1.0},'
        '{"index":1,"relevance_score":' + invalid_json + "}]}"
    )
    requests: list[httpx.Request] = []
    async with _provider(async_mode, response, requests) as model:
        with pytest.raises(ValueError):
            if isinstance(model, AsyncQwen3VLReranker):
                if method == "score":
                    await model.score("query", ["first", "second"])
                else:
                    await model.rank("query", ["first", "second"], top_k=1)
            elif method == "score":
                model.score("query", ["first", "second"])
            else:
                model.rank("query", ["first", "second"], top_k=1)

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/rerank"


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
async def test_qwen_finite_scores_keep_magnitude_order_and_dto_conversion(
    async_mode: bool,
) -> None:
    response = """{"results":[
        {"index":6,"relevance_score":"1e2"},
        {"index":5,"relevance_score":"0.75"},
        {"index":4,"relevance_score":0.0},
        {"index":3,"relevance_score":-0.0},
        {"index":2,"relevance_score":-3.5},
        {"index":1,"relevance_score":-1.7976931348623157e308},
        {"index":0,"relevance_score":1.7976931348623157e308}
    ]}"""
    documents = [f"document-{index}" for index in range(7)]
    requests: list[httpx.Request] = []
    async with _provider(async_mode, response, requests) as model:
        if isinstance(model, AsyncQwen3VLReranker):
            scores = await model.score("query", documents)
            ranked = await model.rank("query", documents)
        else:
            scores = model.score("query", documents)
            ranked = model.rank("query", documents)

    assert scores == [
        sys.float_info.max,
        -sys.float_info.max,
        -3.5,
        -0.0,
        0.0,
        0.75,
        100.0,
    ]
    assert math.copysign(1.0, scores[3]) == -1.0
    assert [item.index for item in ranked] == [0, 6, 5, 3, 4, 2, 1]
    assert [item.score for item in ranked] == [
        sys.float_info.max,
        100.0,
        0.75,
        -0.0,
        0.0,
        -3.5,
        -sys.float_info.max,
    ]
    assert len(requests) == 2


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("multimodal", [False, True], ids=["text", "multimodal"])
async def test_qwen_low_level_score_accepts_one_provider_document(
    async_mode: bool, multimodal: bool
) -> None:
    response = '{"results":[{"index":0,"relevance_score":0.5}]}'
    document = (
        ScoreMultiModalParam(
            content=[ChatCompletionContentPartTextParam(text="one document")]
        )
        if multimodal
        else "one document"
    )
    # Qwen 的低层 DTO 接受单个多模态对象；基类注解尚只声明 Sequence。
    provider_documents = cast("Sequence[str | ScoreMultiModalParam]", document)
    requests: list[httpx.Request] = []
    async with _provider(async_mode, response, requests) as model:
        if isinstance(model, AsyncQwen3VLReranker):
            scores = await model.score("query", provider_documents)
        else:
            scores = model.score("query", provider_documents)

    assert scores == [0.5]
    assert len(requests) == 1
    expected = (
        {"content": [{"text": "one document", "type": "text"}]}
        if multimodal
        else "one document"
    )
    assert json.loads(requests[0].content)["documents"] == expected
