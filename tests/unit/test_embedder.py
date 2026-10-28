from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from comet_rag.content import ContentItem, ImagePart, MediaRef
from comet_rag.embedder.models.openai import AsyncOpenAITextEmbedder
from comet_rag.embedder.types import EmbeddingSpace


def test_wire_mapping_role_prefix_and_client_ownership() -> None:
    async def scenario() -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0, 1]},
                        {"index": 0, "embedding": [1, 0]},
                    ]
                },
            )

        async with httpx.AsyncClient(
            base_url="https://model.test/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            space = EmbeddingSpace(
                "model", 2, "test-deployment", query_prefix="q:", document_prefix="d:"
            )
            model = AsyncOpenAITextEmbedder(client=client, space=space)
            result = await model.embed(
                [ContentItem("a", " x "), ContentItem("b", "y")], role="query"
            )
            assert requests[0]["input"] == ["q: x ", "q:y"]
            assert [(entry.item_id, entry.vector) for entry in result] == [
                ("a", (1.0, 0.0)),
                ("b", (0.0, 1.0)),
            ]
            assert all(entry.space_id == space.space_id for entry in result)
            assert not client.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "entries",
    [
        [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1, 0]}],
        [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": True, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
    ],
)
def test_bad_mapping_fails_the_entire_batch(entries: list[dict[str, object]]) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://model.test/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": entries})
            ),
        ) as client:
            model = AsyncOpenAITextEmbedder(
                client=client, space=EmbeddingSpace("m", 2, "d")
            )
            with pytest.raises(ValueError):
                await model.embed(
                    [ContentItem("a", "x"), ContentItem("b", "y")], role="document"
                )

    asyncio.run(scenario())


def test_budget_and_images_are_rejected_before_requests() -> None:
    async def scenario() -> None:
        def request(request: httpx.Request) -> httpx.Response:
            pytest.fail("invalid input must fail before network")

        async with httpx.AsyncClient(
            base_url="https://model.test/", transport=httpx.MockTransport(request)
        ) as client:
            model = AsyncOpenAITextEmbedder(
                client=client, space=EmbeddingSpace("m", 2, "d"), max_chars=2
            )
            for items in (
                [ContentItem("a", "long")],
                [
                    ContentItem(
                        "a", [ImagePart(MediaRef(data=b"i", media_type="image/png"))]
                    )
                ],
                [ContentItem("a", "x"), ContentItem("a", "y")],
            ):
                with pytest.raises(ValueError):
                    await model.embed(items, role="document")

    asyncio.run(scenario())
