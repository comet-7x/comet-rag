from __future__ import annotations

import httpx
import pytest

from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.reranker.qwen3_vl_reranker import (
    ChatCompletionContentPartImageParam,
    ImageUrlParam,
    Qwen3VLReranker,
    ScoreMultiModalParam,
)


def _multimodal(url: str) -> ScoreMultiModalParam:
    return ScoreMultiModalParam(
        content=[ChatCompletionContentPartImageParam(image_url=ImageUrlParam(url=url))]
    )


@pytest.mark.parametrize("async_mode", [False, True])
async def test_remote_image_url_is_validated_before_model_request(
    async_mode: bool,
) -> None:
    requested: list[str] = []
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    def reject_private(url: str) -> None:
        validated.append(url)
        raise PermissionError("private image URL rejected")

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
        image_url_validator=reject_private,
    )
    query = _multimodal("http://127.0.0.1/metadata")
    try:
        with pytest.raises(CometRAGException, match="private image URL rejected"):
            if async_mode:
                await model.ascore(query, ["document"])
            else:
                model.score(query, ["document"])
        assert validated == ["http://127.0.0.1/metadata"]
        assert requested == []
    finally:
        sync_client.close()
        await async_client.aclose()


def test_base64_image_does_not_require_url_policy() -> None:
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 0.5}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=client,
        image_url_validator=validated.append,
    )
    try:
        assert model.score(_multimodal("data:image/png;base64,AAAA"), ["doc"]) == [0.5]
        assert validated == []
    finally:
        client.close()
