from __future__ import annotations

import json

import httpx

from comet_rag.infrastructure.models.embedding.qwen3_vl_embedding import (
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
                "data": [
                    {"index": 0, "object": "embedding", "embedding": [1.0, 2.0]}
                ],
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
