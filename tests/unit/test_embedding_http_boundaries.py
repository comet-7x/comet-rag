from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from comet_rag.content import ContentItem
from comet_rag.embedder.models.openai import AsyncOpenAITextEmbedder
from comet_rag.embedder.types import EmbeddingSpace

BODY = json.dumps({"data": [{"index": 0, "embedding": [1, 0]}]}).encode()


class Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes = BODY, *, fail: bool = False) -> None:
        self.data = data
        self.fail = fail
        self.read = False
        self.closed = False
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        yield self.data

    async def aclose(self) -> None:
        self.closing.set()
        await self.release.wait()
        self.closed = True
        if self.fail:
            raise OSError("close failed")


@pytest.mark.parametrize("failure", ["success", "status", "budget", "close"])
def test_response_close_drains_repeated_cancellation(failure: str) -> None:
    async def scenario() -> None:
        body = Body(fail=failure == "close")
        body.release.clear()
        async with httpx.AsyncClient(
            base_url="https://model.test/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    503 if failure == "status" else 200,
                    stream=body,
                )
            ),
        ) as client:
            adapter = AsyncOpenAITextEmbedder(
                client=client,
                space=EmbeddingSpace("model", 2, "deployment"),
                max_response_bytes=1 if failure == "budget" else len(BODY),
            )
            task = asyncio.create_task(
                adapter.embed([ContentItem("one", "text")], role="query")
            )
            try:
                await asyncio.wait_for(body.closing.wait(), 2)
                for _ in range(3):
                    task.cancel()
                    for _ in range(5):
                        await asyncio.sleep(0)
                    assert not task.done() and not body.closed
                body.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert body.closed and not client.is_closed
            finally:
                body.release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_cleanup_failure_preserves_response_error() -> None:
    async def scenario() -> None:
        body = Body(fail=True)
        async with httpx.AsyncClient(
            base_url="https://model.test/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, stream=body)
            ),
        ) as client:
            adapter = AsyncOpenAITextEmbedder(
                client=client, space=EmbeddingSpace("m", 2, "d")
            )
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await adapter.embed([ContentItem("one", "text")], role="document")
            assert "embedding response close failed: OSError" in caught.value.__notes__
            assert body.closed and not client.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize("compressed", [False, True])
def test_response_budget_rejects_before_reading_known_oversize_or_compressed_body(
    compressed: bool,
) -> None:
    async def scenario() -> None:
        body = Body(gzip.compress(BODY) if compressed else BODY)
        headers = (
            {"content-encoding": "gzip"}
            if compressed
            else {"content-length": str(len(BODY))}
        )
        async with httpx.AsyncClient(
            base_url="https://model.test/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, stream=body)
            ),
        ) as client:
            adapter = AsyncOpenAITextEmbedder(
                client=client, space=EmbeddingSpace("m", 2, "d"), max_response_bytes=1
            )
            with pytest.raises(ValueError, match="encoding|byte budget"):
                await adapter.embed([ContentItem("one", "text")], role="query")
            assert not body.read and body.closed and not client.is_closed

    asyncio.run(scenario())


def test_batch_failure_cancels_and_drains_sibling_without_dispatching_more() -> None:
    async def scenario() -> None:
        closing, release = asyncio.Event(), asyncio.Event()
        requests: list[str] = []

        class Blocking(Body):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                await asyncio.Event().wait()
                yield BODY

            async def aclose(self) -> None:
                closing.set()
                await release.wait()
                self.closed = True

        sibling = Blocking()

        async def handler(request: httpx.Request) -> httpx.Response:
            text = json.loads(request.content)["input"][0]
            requests.append(text)
            if text == "first":
                while "second" not in requests:
                    await asyncio.sleep(0)
                return httpx.Response(503, stream=Body())
            return httpx.Response(200, stream=sibling)

        async with httpx.AsyncClient(
            base_url="https://model.test/", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = AsyncOpenAITextEmbedder(
                client=client,
                space=EmbeddingSpace("m", 2, "d"),
                batch_size=1,
                concurrency=2,
            )
            task = asyncio.create_task(
                adapter.embed(
                    [ContentItem(text, text) for text in ("first", "second", "third")],
                    role="document",
                )
            )
            try:
                await asyncio.wait_for(closing.wait(), 2)
                assert not task.done() and requests == ["first", "second"]
                release.set()
                with pytest.raises(httpx.HTTPStatusError):
                    await task
                assert sibling.closed and not client.is_closed
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("length", ["-1", "invalid", "0", str(len(BODY) + 1)])
def test_response_rejects_invalid_or_mismatched_content_length(length: str) -> None:
    async def scenario() -> None:
        body = Body()
        async with httpx.AsyncClient(
            base_url="https://model.test/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-length": length},
                    stream=body,
                )
            ),
        ) as client:
            adapter = AsyncOpenAITextEmbedder(
                client=client, space=EmbeddingSpace("m", 2, "d")
            )
            with pytest.raises(ValueError, match="Content-Length"):
                await adapter.embed([ContentItem("one", "text")], role="document")
            assert body.closed and not client.is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [len(BODY), len(BODY) - 1])
def test_stream_enforces_exact_response_budget(limit: int) -> None:
    async def scenario() -> None:
        body = Body()
        async with httpx.AsyncClient(
            base_url="https://model.test/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=body)
            ),
        ) as client:
            adapter = AsyncOpenAITextEmbedder(
                client=client,
                space=EmbeddingSpace("m", 2, "d"),
                max_response_bytes=limit,
            )
            if limit < len(BODY):
                with pytest.raises(ValueError, match="byte budget"):
                    await adapter.embed([ContentItem("one", "text")], role="document")
            else:
                result = await adapter.embed(
                    [ContentItem("one", "text")], role="document"
                )
                assert result[0].vector == (1.0, 0.0)
            assert body.closed and not client.is_closed

    asyncio.run(scenario())
