from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.documents.codec import dumps, loads
from comet_rag.documents.parsers import mineru
from comet_rag.documents.parsers.base import AsyncDocumentParser
from comet_rag.documents.parsers.mineru import AsyncMinerUParser
from comet_rag.documents.projection import project_document
from comet_rag.documents.schemas import SourceInfo
from comet_rag.sources.types import LocalResource
from tests.fixtures.pdf import minimal_pdf_bytes


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False
        self.read_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def resource(path: Path, data: bytes | None = None) -> LocalResource:
    actual = data if data is not None else minimal_pdf_bytes()
    path.write_bytes(actual)
    return LocalResource(
        path,
        SourceInfo(
            "logical-pdf",
            hashlib.sha256(actual).hexdigest(),
            source_version="source-version",
        ),
        original_name="../../untrusted-name.txt",
        media_type="application/octet-stream",
        size_bytes=len(actual),
    )


def response_payload(
    markdown: str = " # Title \n\n| A |\n|---|\n| 001 |\n![image](images/missing.jpg)\n",
) -> dict[str, Any]:
    return {
        "task_id": "task_test",
        "status": "completed",
        "tier": "standard",
        "output_formats": ["markdown"],
        "files": [
            {
                "name": "document.pdf",
                "status": "completed",
                "error": None,
                "parse": {"parser_version": "4.0.12"},
                "content": {"markdown": markdown},
            }
        ],
    }


def parser(client: httpx.AsyncClient, **kwargs: Any) -> AsyncMinerUParser:
    return AsyncMinerUParser(
        client=client,
        endpoint="https://mineru.example/v1/file_parse",
        expected_version="4.0.12",
        **kwargs,
    )


def test_pinned_official_http_request_and_raw_markdown_partial_mapping(
    tmp_path: Path,
) -> None:
    item = resource(tmp_path / "source")
    original = item.path.read_bytes()
    markdown = response_payload()["files"][0]["content"]["markdown"]
    requests: list[httpx.Request] = []
    stream = Stream([json.dumps(response_payload()).encode()])

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        assert request.method == "POST" and request.url.path == "/v1/file_parse"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-type"] == "application/octet-stream"
        assert int(request.headers["content-length"]) == len(original)
        assert body == original
        assert dict(request.url.params) == {
            "tier": "standard",
            "ocr_mode": "auto",
            "output_formats": "markdown",
            "response_format": "json",
            "wait_timeout": "300",
            "filename": "document.pdf",
            "sha256sum": hashlib.sha256(original).hexdigest(),
        }
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=stream
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as client:
            adapter: AsyncDocumentParser = parser(client)
            result = await adapter.parse(item, output_dir=tmp_path / "unused-output")
            assert result.document.source is item.source
            assert result.document.parsing.engine == "mineru-http"
            assert result.document.parsing.version == "4.0.12"
            assert result.document.parsing.config == {
                "tier": "standard",
                "ocr_mode": "auto",
                "output": "markdown-only",
                "mapping_version": 2,
            }
            assert (
                len(result.document.blocks) == 1
                and result.document.blocks[0].kind == "text"
            )
            assert project_document(result.document).text == markdown
            assert result.document.assets == () and result.document.pages == ()
            assert result.document.blocks[0].locations == ()
            assert result.report.status == "partial" and result.report.coverage is None
            assert [(issue.code, issue.severity) for issue in result.report.issues] == [
                ("mineru_markdown_only", "warning")
            ]
            assert "image assets" in result.report.issues[0].message
            assert loads(dumps(result)) == result
            assert stream.closed and not client.is_closed
            assert not (tmp_path / "unused-output").exists()
            assert item.path.read_bytes() == original
            renamed = replace(item, original_name="other.pdf")
            again = await adapter.parse(renamed, output_dir=tmp_path / "different")
            assert again.document.document_id == result.document.document_id
        assert client.is_closed

    asyncio.run(run())
    assert len(requests) == 2


@pytest.mark.parametrize(
    "data",
    [
        b"not PDF",
        b"%PDF-1.7\ntruncated",
        b"prefix%PDF-1.7\n%%EOF",
        b"%PDF-1.7\n%%EOF\ntrailing",
    ],
)
def test_pdf_signature_and_end_marker_are_checked_before_upload(
    tmp_path: Path, data: bytes
) -> None:
    item = resource(tmp_path / "input", data)

    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid PDF must not be uploaded")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ValueError, match="signature.*EOF"):
                await parser(client).parse(item, output_dir=tmp_path)

    asyncio.run(run())
    assert item.path.read_bytes() == data


def test_complete_input_hash_size_and_byte_budget_are_validated_before_upload(
    tmp_path: Path,
) -> None:
    item = resource(tmp_path / "input")
    original = item.path.read_bytes()

    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid resource must not be uploaded")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for pending, kwargs, message in (
                (item, {"max_bytes": len(original) - 1}, "exceeds"),
                (
                    replace(item, size_bytes=None),
                    {"max_bytes": len(original) - 1},
                    "exceeds",
                ),
                (replace(item, size_bytes=len(original) + 1), {}, "size changed"),
                (
                    replace(item, source=replace(item.source, content_hash="0" * 64)),
                    {},
                    "content_hash",
                ),
            ):
                with pytest.raises(ValueError, match=message):
                    await parser(client, **kwargs).parse(pending, output_dir=tmp_path)
            item.path.write_bytes(original.replace(b"Hello", b"Other"))
            with pytest.raises(ValueError, match="content_hash"):
                await parser(client).parse(item, output_dir=tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        {"task_id": ""},
        {"task_id": 3},
        {"status": "partial"},
        {"status": "failed"},
        {"status": "canceled"},
        {"status": "running"},
        {"tier": "flash"},
        {"output_formats": ["markdown", "middle_json"]},
        {"files": []},
        {"files": [None]},
        {"files": {}},
        {"files": [response_payload()["files"][0]] * 2},
    ],
)
def test_unknown_missing_or_invalid_response_schema_fails(
    tmp_path: Path, mutation: dict[str, Any]
) -> None:
    payload = {**response_payload(), **mutation}
    item = resource(tmp_path / "input")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ) as client:
            with pytest.raises(ValueError):
                await parser(client).parse(item, output_dir=tmp_path)
            assert not client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"\xff",
        b'{"backend":"pipeline","backend":"pipeline"}',
        b'{"x":NaN}',
    ],
)
def test_malformed_duplicate_or_nonfinite_json_is_rejected(
    tmp_path: Path, body: bytes
) -> None:
    item = resource(tmp_path / "input")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=body, headers={"content-type": "application/json"}
                )
            )
        ) as client:
            with pytest.raises(ValueError):
                await parser(client).parse(item, output_dir=tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        {"name": "other.pdf"},
        {"status": "failed"},
        {"error": {"code": "parse_failed"}},
        {"parse": None},
        {"parse": {"parser_version": "4.0.11"}},
        {"parse": {"parser_version": True}},
        {"content": None},
        {"content": {}},
        {"content": {"markdown": None}},
        {"content": {"markdown": ["text"]}},
        {"content": {"markdown": "text", "images": {}}},
    ],
)
def test_invalid_file_results_never_map_to_partial_success(
    tmp_path: Path, mutation: dict[str, Any]
) -> None:
    item = resource(tmp_path / "input")
    payload = response_payload()
    payload["files"][0].update(mutation)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ) as client:
            with pytest.raises(ValueError):
                await parser(client).parse(item, output_dir=tmp_path)

    asyncio.run(run())


def test_wait_timeout_reports_task_without_resubmit_or_poll(tmp_path: Path) -> None:
    item = resource(tmp_path / "input")
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            202, json={"task_id": "task_still_running", "status": "running"}
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(TimeoutError, match="task_still_running.*running"):
                await parser(client, wait_timeout=1).parse(item, output_dir=tmp_path)
            assert len(calls) == 1 and not client.is_closed
            assert calls[0].url.params["wait_timeout"] == "1"

    asyncio.run(run())


def test_normal_protocol_metadata_is_accepted(tmp_path: Path) -> None:
    item = resource(tmp_path / "input")
    payload = response_payload("text")
    payload.update(
        status_url="/v1/tasks/task_test", result_url="/v1/tasks/task_test/result"
    )
    payload["files"][0].update(
        file_id="file_test", output_files={"markdown": "output_test"}
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ) as client:
            result = await parser(client).parse(item, output_dir=tmp_path)
            assert project_document(result.document).text == "text"

    asyncio.run(run())


def test_empty_markdown_is_retained_as_one_text_block_with_partial_report(
    tmp_path: Path,
) -> None:
    item = resource(tmp_path / "input")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=response_payload(""))
            )
        ) as client:
            result = await parser(client).parse(item, output_dir=tmp_path)
            assert (
                len(result.document.blocks) == 1
                and project_document(result.document).text == ""
            )
            assert result.report.status == "partial" and result.report.coverage is None

    asyncio.run(run())


@pytest.mark.parametrize("status", [302, 307, 400, 503])
def test_http_failures_and_redirects_do_not_follow_or_return_partial_success(
    tmp_path: Path, status: int
) -> None:
    item = resource(tmp_path / "input")
    calls: list[str] = []
    stream = Stream([b"failure"])

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            status,
            headers={"location": "https://elsewhere.invalid/file_parse"},
            stream=stream,
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), follow_redirects=True
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await parser(client).parse(item, output_dir=tmp_path)
            assert len(calls) == 1 and stream.closed and not client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/html"},
        {"content-type": "application/json", "content-length": "bad"},
        {"content-type": "application/json", "content-length": "1000000"},
        {"content-type": "application/json", "content-length": "1"},
        {"content-type": "application/json", "content-encoding": "gzip"},
    ],
)
def test_response_headers_and_stream_budget_are_enforced_and_closed(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    item = resource(tmp_path / "input")
    stream = Stream([b"{}"])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers=headers, stream=stream)
            )
        ) as client:
            with pytest.raises(ValueError):
                await parser(client, max_response_bytes=128).parse(
                    item, output_dir=tmp_path
                )
            assert stream.closed and not client.is_closed

    asyncio.run(run())


def test_stream_budget_without_content_length_stops_response_and_closes_it(
    tmp_path: Path,
) -> None:
    item = resource(tmp_path / "input")
    stream = Stream([b"x" * 65536, b"y"])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=stream
                )
            )
        ) as client:
            with pytest.raises(ValueError, match="byte budget"):
                await parser(client, max_response_bytes=1024).parse(
                    item, output_dir=tmp_path
                )
            assert stream.read_count == 1 and stream.closed

    asyncio.run(run())


def test_cancellation_during_http_stream_propagates_and_closes_only_response(
    tmp_path: Path,
) -> None:
    item = resource(tmp_path / "input")

    async def run() -> None:
        started = asyncio.Event()

        class BlockingStream(Stream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                started.set()
                await asyncio.Future()
                yield b"unreachable"

        stream = BlockingStream([])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=stream
                )
            )
        ) as client:
            task = asyncio.create_task(parser(client).parse(item, output_dir=tmp_path))
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed and not client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["status", "stream", "cancel", "close_only"])
def test_response_close_failure_preserves_active_error(
    tmp_path: Path, failure: str
) -> None:
    item = resource(tmp_path / "input")

    async def run() -> None:
        entered = asyncio.Event()
        original = httpx.ReadError("original stream failure")

        class CloseFailure(Stream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                if failure == "stream":
                    raise original
                if failure == "cancel":
                    entered.set()
                    await asyncio.Future()
                yield json.dumps(response_payload()).encode()

            async def aclose(self) -> None:
                self.closed = True
                raise OSError("close failed")

        stream = CloseFailure([])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    503 if failure == "status" else 200,
                    headers={"content-type": "application/json"},
                    stream=stream,
                )
            )
        ) as client:
            work = asyncio.create_task(parser(client).parse(item, output_dir=tmp_path))
            if failure == "cancel":
                await asyncio.wait_for(entered.wait(), 1)
                work.cancel()
            expected = {
                "status": httpx.HTTPStatusError,
                "stream": httpx.ReadError,
                "cancel": asyncio.CancelledError,
                "close_only": OSError,
            }[failure]
            with pytest.raises(expected) as caught:
                await work
            if failure == "stream":
                assert caught.value is original
            if failure != "close_only":
                assert "MinerU response close failed: OSError" in caught.value.__notes__
            assert stream.closed and not client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["status", "read", "success", "close_failure"])
def test_repeated_cancellation_drains_response_close(
    tmp_path: Path, failure: str
) -> None:
    item = resource(tmp_path / "input")

    async def run() -> None:
        reading = asyncio.Event()
        closing = asyncio.Event()
        release = asyncio.Event()

        class BlockingClose(Stream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                if failure == "read":
                    reading.set()
                    await asyncio.Event().wait()
                yield json.dumps(response_payload()).encode()

            async def aclose(self) -> None:
                closing.set()
                await release.wait()
                self.closed = True
                if failure == "close_failure":
                    raise OSError("close failed")

        stream = BlockingClose([])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    503 if failure == "status" else 200,
                    headers={"content-type": "application/json"},
                    stream=stream,
                )
            )
        ) as client:
            task = asyncio.create_task(parser(client).parse(item, output_dir=tmp_path))
            try:
                if failure == "read":
                    await asyncio.wait_for(reading.wait(), 3)
                    task.cancel()
                await asyncio.wait_for(closing.wait(), 3)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                release.set()
                expected = (
                    httpx.HTTPStatusError
                    if failure == "status"
                    else asyncio.CancelledError
                )
                with pytest.raises(expected) as caught:
                    await task
                assert stream.closed and not client.is_closed
                if failure == "close_failure":
                    assert (
                        "MinerU response close failed: OSError"
                        in caught.value.__notes__
                    )
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        assert item.path.read_bytes() == minimal_pdf_bytes()

    asyncio.run(run())


def test_cancelled_input_read_is_drained_and_never_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = resource(tmp_path / "input")
    release = threading.Event()
    finished = threading.Event()
    original_read = mineru._read_resource

    async def run() -> None:
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocked_read(value: LocalResource, limit: int) -> bytes:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            result = original_read(value, limit)
            finished.set()
            return result

        def handle(request: httpx.Request) -> httpx.Response:
            pytest.fail("cancelled read must not proceed to upload")

        monkeypatch.setattr(mineru, "_read_resource", blocked_read)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(parser(client).parse(item, output_dir=tmp_path))
            try:
                await asyncio.wait_for(started.wait(), 3)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not finished.is_set()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert finished.is_set() and not client.is_closed
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "endpoint",
    [
        "/file_parse",
        "ftp://mineru/file_parse",
        "https://mineru/parse",
        "https://user:pass@mineru/v1/file_parse",  # pragma: allowlist secret
        "https://mineru/v1/file_parse?token=x",
        "https://mineru/v1/file_parse#fragment",
    ],
)
def test_endpoint_configuration_rejects_unverified_or_credentialed_urls(
    endpoint: str,
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(ValueError, match="endpoint"):
                AsyncMinerUParser(
                    client=client, endpoint=endpoint, expected_version="4.0.12"
                )

    asyncio.run(run())


def test_transport_and_response_stream_failures_propagate_without_fake_partial_document(
    tmp_path: Path,
) -> None:
    item = resource(tmp_path / "input")

    async def run() -> None:
        error = httpx.ConnectError("connection unavailable")

        def unavailable(request: httpx.Request) -> httpx.Response:
            raise error

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(unavailable)
        ) as client:
            with pytest.raises(httpx.ConnectError) as caught:
                await parser(client).parse(item, output_dir=tmp_path)
            assert caught.value is error and not client.is_closed

        class FailedStream(Stream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b'{"backend":'
                raise httpx.ReadError("stream interrupted")

        stream = FailedStream([])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=stream
                )
            )
        ) as client:
            with pytest.raises(httpx.ReadError, match="interrupted"):
                await parser(client).parse(item, output_dir=tmp_path)
            assert stream.closed and not client.is_closed

    asyncio.run(run())


def test_exact_input_and_response_budgets_allow_a_complete_response(
    tmp_path: Path,
) -> None:
    item = resource(tmp_path / "input")
    body = json.dumps(response_payload("exact")).encode()
    stream = Stream([body[:5], body[5:]])

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={
                        "content-type": "application/json",
                        "content-length": str(len(body)),
                    },
                    stream=stream,
                )
            )
        ) as client:
            result = await parser(
                client,
                max_bytes=len(item.path.read_bytes()),
                max_response_bytes=len(body),
            ).parse(item, output_dir=tmp_path)
            assert project_document(result.document).text == "exact" and stream.closed

    asyncio.run(run())


def test_configuration_requires_deployed_version_tier_mode_and_positive_limits() -> (
    None
):
    async def run() -> None:
        async with httpx.AsyncClient(trust_env=False) as client:
            for fields in (
                {"expected_version": "unknown"},
                {"expected_version": "2.6.8"},
                {"expected_version": "5.0.0"},
                {"tier": "unknown"},
                {"ocr_mode": "guess"},
                {"wait_timeout": 0},
                {"wait_timeout": 3601},
                {"wait_timeout": True},
                {"max_bytes": 0},
                {"max_response_bytes": True},
            ):
                config: dict[str, Any] = {
                    "client": client,
                    "endpoint": "https://mineru/v1/file_parse",
                    "expected_version": "4.0.12",
                    **fields,
                }
                with pytest.raises(ValueError):
                    AsyncMinerUParser(**config)

    asyncio.run(run())
