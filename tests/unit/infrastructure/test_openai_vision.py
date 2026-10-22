from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.vision import (
    AsyncOpenAIVisionModel,
    OpenAIVisionModel,
)


@pytest.mark.parametrize("content", ["a cat", None])
async def test_vision_models_share_messages_and_response_semantics(
    content: str | None,
) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": "vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    with OpenAI(
        base_url="https://model.invalid/v1",
        api_key="test",
        http_client=httpx.Client(transport=transport),
    ) as client:
        model = OpenAIVisionModel(
            base_url="https://model.invalid/v1",
            api_key="test",
            model_name="vision",
            system_prompt="Describe briefly.",
            client=client,
        )
        assert model.describe("Y2F0", "image/png", temperature=0.2) == (content or "")
    async with AsyncOpenAI(
        base_url="https://model.invalid/v1",
        api_key="test",
        http_client=httpx.AsyncClient(transport=transport),
    ) as client:
        async_model = AsyncOpenAIVisionModel(
            base_url="https://model.invalid/v1",
            api_key="test",
            model_name="vision",
            system_prompt="Describe briefly.",
            client=client,
        )
        assert await async_model.describe("Y2F0", "image/png", temperature=0.2) == (
            content or ""
        )
    assert (
        payloads
        == [
            {
                "model": "vision",
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "Describe briefly."},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,Y2F0"},
                            }
                        ],
                    },
                ],
            }
        ]
        * 2
    )


async def test_vision_upstream_failure_is_wrapped_in_both_modes() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"error": "unavailable"})
    )
    with OpenAI(
        base_url="https://model.invalid/v1",
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    ) as client:
        model = OpenAIVisionModel(
            base_url="https://model.invalid/v1",
            api_key="test",
            model_name="vision",
            client=client,
        )
        with pytest.raises(CometRAGException, match="503"):
            model.describe("Y2F0", "image/png")
    async with AsyncOpenAI(
        base_url="https://model.invalid/v1",
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport),
    ) as client:
        async_model = AsyncOpenAIVisionModel(
            base_url="https://model.invalid/v1",
            api_key="test",
            model_name="vision",
            client=client,
        )
        with pytest.raises(CometRAGException, match="503"):
            await async_model.describe("Y2F0", "image/png")
