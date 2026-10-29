from __future__ import annotations

import asyncio
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from comet_rag.content import ContentItem
from comet_rag.embedder.models.openai import AsyncOpenAITextEmbedder
from comet_rag.embedder.types import EmbeddingSpace

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("read_in_hook", [False, True])
def test_loopback_embedding_wire_mapping_and_response_budget(
    read_in_hook: bool,
) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.headers["Accept-Encoding"] == "identity"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            data = json.dumps(
                {
                    "data": [
                        {"index": index, "embedding": [index, 1]}
                        for index in reversed(range(len(body["input"])))
                    ]
                }
            ).encode()
            if self.path == "/compressed":
                data = gzip.compress(data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if self.path != "/stream":
                self.send_header("Content-Length", str(len(data)))
            if self.path == "/compressed":
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario() -> None:
        async def read_response(response: httpx.Response) -> None:
            await response.aread()

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{server.server_port}/",
            trust_env=False,
            event_hooks={"response": [read_response]} if read_in_hook else None,
        ) as client:
            space = EmbeddingSpace(
                "fixture", 2, "loopback", query_prefix="q:", document_prefix="d:"
            )
            items = [ContentItem("left", " x "), ContentItem("right", "y")]
            adapter = AsyncOpenAITextEmbedder(client=client, space=space)
            result = await adapter.embed(items, role="query")
            assert [(item.item_id, item.vector) for item in result] == [
                ("left", (0.0, 1.0)),
                ("right", (1.0, 1.0)),
            ]
            assert requests[-1] == {
                "model": "fixture",
                "input": ["q: x ", "q:y"],
                "encoding_format": "float",
            }
            for endpoint in ("embeddings", "stream", "compressed"):
                bounded = AsyncOpenAITextEmbedder(
                    client=client, space=space, endpoint=endpoint, max_response_bytes=1
                )
                with pytest.raises(ValueError, match="encoding|byte budget"):
                    await bounded.embed(items, role="document")
                assert requests[-1]["input"] == ["d: x ", "d:y"]
            assert not client.is_closed

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
