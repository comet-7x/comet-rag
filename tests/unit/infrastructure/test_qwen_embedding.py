from __future__ import annotations

import json

import httpx
import pytest

from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.embedding.qwen3_vl_embedding import (
    EmbeddingData,
    Qwen3VLEmbeddingModel,
)


async def test_qwen_adapter_accepts_base_text_contract() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": [1.0, 2.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    try:
        assert await model.aembed("plain service text") == [1.0, 2.0]
        assert payloads[0]["messages"][1]["content"][0]["text"] == (
            "plain service text"
        )
    finally:
        sync_client.close()
        await async_client.aclose()


@pytest.mark.parametrize("operation", ["embed", "aembed", "tokenize", "atokenize"])
async def test_remote_image_url_is_validated_before_model_request(
    operation: str,
) -> None:
    requested: list[str] = []
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(500)

    def reject_private(url: str) -> None:
        validated.append(url)
        raise PermissionError("private image URL rejected")

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
        image_url_validator=reject_private,
    )
    data = EmbeddingData(image_url="http://127.0.0.1/metadata")
    try:
        with pytest.raises(CometRAGException, match="private image URL rejected"):
            if operation == "embed":
                model.embed(data)
            elif operation == "aembed":
                await model.aembed(data)
            elif operation == "tokenize":
                model.tokenize(data)
            else:
                await model.atokenize(data)

        assert validated == ["http://127.0.0.1/metadata"]
        assert requested == []
    finally:
        sync_client.close()
        await async_client.aclose()


async def test_empty_embedding_response_is_reported_as_adapter_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=client,
    )
    try:
        with pytest.raises(CometRAGException, match="data"):
            model.embed("text")
    finally:
        await model.aclose()
        client.close()


async def test_continue_final_message_controls_assistant_placeholder() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if request.url.path.endswith("/tokenize"):
            return httpx.Response(
                200,
                json={"count": 1, "max_model_len": 1024, "tokens": [1]},
            )
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": [1.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    data = EmbeddingData(text="plain text")
    try:
        model.embed(data, continue_final_message=False)
        await model.aembed(data, continue_final_message=False)
        model.tokenize(data, continue_final_message=False)
        await model.atokenize(data, continue_final_message=False)
        model.embed(data, continue_final_message=True)

        assert all(
            payload["messages"][-1]["role"] == "user" for payload in payloads[:4]
        )
        assert payloads[4]["messages"][-1]["role"] == "assistant"
    finally:
        sync_client.close()
        await async_client.aclose()
