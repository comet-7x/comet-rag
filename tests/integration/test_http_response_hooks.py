from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.documents.parsers.mineru import AsyncMinerUParser
from comet_rag.documents.projection import project_document
from comet_rag.sources import HttpSource, SourceError, SourceResourceLimitExceeded
from comet_rag.sources.http import AsyncHttpSourceResolver
from tests.unit.test_mineru_parser import resource, response_payload

pytestmark = pytest.mark.integration
BODY = b"hello\r\n  preserved"


@pytest.fixture
def hook_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.respond(BODY, "text/plain")

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.respond(
                json.dumps(response_payload("parsed PDF")).encode(), "application/json"
            )

        def respond(self, data: bytes, media_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            if not self.path.startswith("/stream"):
                length = len(data) + (1 if self.path.startswith("/wrong-length") else 0)
                self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("read_in_hook", [False, True])
@pytest.mark.parametrize("path", ["/length", "/stream", "/wrong-length"])
def test_http_source_response_hooks_preserve_bytes_and_limits(
    hook_server: str, tmp_path: Path, read_in_hook: bool, path: str
) -> None:
    async def run() -> None:
        async def read(response: httpx.Response) -> None:
            await response.aread()

        async with httpx.AsyncClient(
            trust_env=False,
            timeout=3,
            event_hooks={"response": [read]} if read_in_hook else None,
        ) as client:
            resolver = AsyncHttpSourceResolver(
                client,
                policy=lambda source: True,
                temp_dir=tmp_path,
            )
            if path == "/wrong-length":
                with pytest.raises(SourceError):
                    async with resolver.resolve(HttpSource(hook_server + path)):
                        pytest.fail("incomplete body accepted")
            else:
                async with resolver.resolve(HttpSource(hook_server + path)) as item:
                    assert item.path.read_bytes() == BODY
                bounded = AsyncHttpSourceResolver(
                    client,
                    policy=lambda source: True,
                    max_bytes=len(BODY) - 1,
                    temp_dir=tmp_path,
                )
                with pytest.raises(SourceResourceLimitExceeded):
                    async with bounded.resolve(HttpSource(hook_server + path)):
                        pytest.fail("oversize body accepted")
            assert not client.is_closed
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize("read_in_hook", [False, True])
@pytest.mark.parametrize("path", ["/length", "/stream", "/wrong-length"])
def test_mineru_response_hooks_preserve_result_and_limits(
    hook_server: str, tmp_path: Path, read_in_hook: bool, path: str
) -> None:
    item = resource(tmp_path / "source.pdf")

    async def run() -> None:
        async def read(response: httpx.Response) -> None:
            await response.aread()

        async with httpx.AsyncClient(
            trust_env=False,
            timeout=3,
            event_hooks={"response": [read]} if read_in_hook else None,
        ) as client:
            options = {
                "client": client,
                "endpoint": hook_server + path + "/v1/file_parse",
                "expected_version": "4.0.12",
            }
            parser = AsyncMinerUParser(**options)
            if path == "/wrong-length":
                with pytest.raises((ValueError, httpx.HTTPError)):
                    await parser.parse(item, output_dir=tmp_path / "output")
            else:
                parsed = await parser.parse(item, output_dir=tmp_path / "output")
                assert project_document(parsed.document).text == "parsed PDF"
                bounded = AsyncMinerUParser(**options, max_response_bytes=1)
                with pytest.raises(ValueError, match="byte budget"):
                    await bounded.parse(item, output_dir=tmp_path / "output")
            assert not client.is_closed

    asyncio.run(run())
