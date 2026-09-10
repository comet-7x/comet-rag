from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.providers.document import (
    MINERU_API_PROTOCOL_VERSION,
    MinerUDocumentExtractor,
)
from comet_rag.ports import (
    DocumentExtractorPort,
    DocumentProtocolError,
    DocumentResourceLimitExceeded,
    DocumentUpstreamError,
    ExtractedDocument,
    RetryableDocumentUpstreamError,
)
from tests.contracts.document_extractor import DocumentExtractorContract

BASE_URL = "https://mineru.test/api"
MARKDOWN = "# 标题\n\n正文包含 $E=mc^2$。"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += delay

    async def asleep(self, delay: float) -> None:
        self.now += delay


class ScriptedMinerU:
    def __init__(
        self,
        *,
        statuses: tuple[str, ...] = ("pending", "processing", "completed"),
        health: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        submission: dict[str, Any] | None = None,
        missing_task_ids: set[str] | None = None,
    ) -> None:
        self.statuses = statuses
        self.health = (
            health
            if health is not None
            else {
                "status": "healthy",
                "version": "3.3.0",
                "protocol_version": MINERU_API_PROTOCOL_VERSION,
            }
        )
        self.result = (
            result
            if result is not None
            else {
                "backend": "pipeline",
                "version": "3.3.0",
                "results": {"sample": {"md_content": MARKDOWN}},
            }
        )
        self.submission = submission
        self.missing_task_ids = missing_task_ids or set()
        self.requests: list[httpx.Request] = []
        self.submission_bodies: list[bytes] = []
        self.submissions = 0
        self.polls: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/health"):
            return httpx.Response(200, json=self.health)
        if request.method == "POST" and path.endswith("/tasks"):
            self.submissions += 1
            self.submission_bodies.append(request.content)
            task_id = f"task-{self.submissions}"
            payload = (
                self.submission
                if self.submission is not None
                else {
                    "task_id": task_id,
                    "status": "pending",
                    # 适配器不得跟随服务端返回的任意地址。
                    "status_url": f"https://attacker.invalid/tasks/{task_id}",
                    "result_url": f"https://attacker.invalid/tasks/{task_id}/result",
                }
            )
            return httpx.Response(202, json=payload)
        if request.method == "GET" and path.endswith("/result"):
            return httpx.Response(200, json=self.result)
        if request.method == "GET" and "/tasks/" in path:
            task_id = path.rsplit("/", 1)[-1]
            if task_id in self.missing_task_ids:
                return httpx.Response(404, json={"detail": "task not found"})
            index = self.polls.get(task_id, 0)
            status = self.statuses[min(index, len(self.statuses) - 1)]
            self.polls[task_id] = index + 1
            return httpx.Response(
                200,
                json={
                    "task_id": task_id,
                    "status": status,
                    "error": "模型执行失败" if status == "failed" else None,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})


def _extractor(
    server: Callable[[httpx.Request], httpx.Response],
    *,
    sleeps: list[float] | None = None,
    async_sleeps: list[float] | None = None,
    **options: Any,
) -> MinerUDocumentExtractor:
    transport = httpx.MockTransport(server)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)

    def sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    async def asleep(delay: float) -> None:
        if async_sleeps is not None:
            async_sleeps.append(delay)

    kwargs = {
        "poll_interval_seconds": 0.25,
        "sync_client": sync_client,
        "async_client": async_client,
        "sleep": sleep,
        "asleep": asleep,
        **options,
    }
    return MinerUDocumentExtractor(BASE_URL, **kwargs)


@pytest.fixture
def document_path(tmp_path: Path) -> Path:
    return _write_pdf(tmp_path)


def _write_pdf(directory: Path) -> Path:
    path = directory / "sample.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture")
    return path


class TestMinerUDocumentExtractor(DocumentExtractorContract):
    @pytest.fixture
    def extractor(self) -> DocumentExtractorPort:
        return _extractor(ScriptedMinerU())

    @pytest.fixture
    def document_path(self, tmp_path: Path) -> Path:
        return _write_pdf(tmp_path)

    @pytest.fixture
    def expected_document(self) -> ExtractedDocument:
        return ExtractedDocument(
            markdown=MARKDOWN,
            metadata={
                "provider": "mineru",
                "backend": "pipeline",
                "version": "3.3.0",
                "protocol_version": MINERU_API_PROTOCOL_VERSION,
            },
        )


async def test_sync_and_async_paths_handle_queued_states(
    document_path: Path,
) -> None:
    server = ScriptedMinerU()
    sync_sleeps: list[float] = []
    async_sleeps: list[float] = []
    extractor = _extractor(server, sleeps=sync_sleeps, async_sleeps=async_sleeps)

    sync_result = extractor.extract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    async_result = await extractor.aextract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )

    assert sync_result == async_result
    assert sync_sleeps == [0.25, 0.25]
    assert async_sleeps == [0.25, 0.25]
    assert all(request.url.host == "mineru.test" for request in server.requests)


def test_submission_streams_one_pdf_and_sends_every_output_flag(
    document_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ScriptedMinerU(statuses=("completed",))
    extractor = _extractor(server)

    def forbidden_read_bytes(self: Path) -> bytes:
        raise AssertionError("上传不得一次性 read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    extractor.extract(
        document_path, filename="original.pdf", media_type="application/pdf"
    )

    body = server.submission_bodies[0]
    assert b'name="files"; filename="original.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert b"%PDF-1.7\nfixture" in body
    expected = {
        "lang_list": "ch",
        "backend": "pipeline",
        "parse_method": "auto",
        "formula_enable": "true",
        "table_enable": "true",
        "return_md": "true",
        "return_middle_json": "false",
        "return_model_output": "false",
        "return_content_list": "false",
        "return_images": "false",
        "response_format_zip": "false",
        "return_original_file": "false",
        "client_side_output_generation": "false",
    }
    for field, value in expected.items():
        fragment = f'name="{field}"\r\n\r\n{value}\r\n'.encode()
        assert fragment in body


@pytest.mark.parametrize(
    ("health", "message"),
    [
        (
            {"status": "unhealthy", "version": "3.3.0", "protocol_version": 2},
            "healthy",
        ),
        (
            {"status": "healthy", "version": "3.3.0", "protocol_version": 1},
            "protocol_version",
        ),
        ({"status": "healthy", "protocol_version": 2}, "version"),
    ],
)
def test_health_contract_is_validated(
    document_path: Path, health: dict[str, Any], message: str
) -> None:
    extractor = _extractor(ScriptedMinerU(health=health))
    error = (
        DocumentUpstreamError
        if health["status"] == "unhealthy"
        else DocumentProtocolError
    )

    with pytest.raises(error, match=message):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


def test_failed_task_surfaces_remote_error(document_path: Path) -> None:
    extractor = _extractor(ScriptedMinerU(statuses=("failed",)))

    with pytest.raises(DocumentUpstreamError, match="模型执行失败"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


@pytest.mark.parametrize(
    "result",
    [
        {"results": {}},
        {"results": {"one": {"md_content": "一"}, "two": {"md_content": "二"}}},
        {"results": {"sample": {}}},
        {"results": {"sample": "不是 object"}},
    ],
)
def test_result_requires_exactly_one_markdown(
    document_path: Path, result: dict[str, Any]
) -> None:
    extractor = _extractor(ScriptedMinerU(statuses=("completed",), result=result))

    with pytest.raises(DocumentProtocolError):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


@pytest.mark.parametrize("status", ["cancelled", "unknown", ""])
def test_unknown_task_status_is_protocol_error(
    document_path: Path, status: str
) -> None:
    extractor = _extractor(ScriptedMinerU(statuses=(status,)))

    with pytest.raises(DocumentProtocolError, match="未知任务状态"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


def test_submission_requires_non_empty_task_id(document_path: Path) -> None:
    extractor = _extractor(
        ScriptedMinerU(submission={"task_id": "", "status": "pending"})
    )

    with pytest.raises(DocumentProtocolError, match="task_id"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


async def test_clients_are_reused_and_injected_clients_remain_open(
    document_path: Path,
) -> None:
    server = ScriptedMinerU(statuses=("completed",))
    extractor = _extractor(server)
    sync_client = extractor.sync_client
    async_client = extractor.async_client

    extractor.extract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    extractor.extract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    await extractor.aextract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    await extractor.aextract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    await extractor.aclose()

    assert extractor.sync_client is sync_client
    assert extractor.async_client is async_client
    assert server.submissions == 4
    assert not sync_client.is_closed
    assert not async_client.is_closed
    sync_client.close()
    await async_client.aclose()


async def test_internal_clients_are_closed_idempotently() -> None:
    extractor = MinerUDocumentExtractor(BASE_URL)

    await extractor.aclose()
    await extractor.aclose()

    assert extractor.sync_client.is_closed
    assert extractor.async_client.is_closed


@pytest.mark.parametrize(
    ("status_code", "error"),
    [
        (408, RetryableDocumentUpstreamError),
        (429, RetryableDocumentUpstreamError),
        (500, RetryableDocumentUpstreamError),
        (503, RetryableDocumentUpstreamError),
        (400, DocumentUpstreamError),
        (401, DocumentUpstreamError),
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http_statuses_are_mapped_at_the_adapter_boundary(
    document_path: Path,
    status_code: int,
    error: type[DocumentUpstreamError],
    asynchronous: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "upstream rejected"})

    extractor = _extractor(handler)

    with pytest.raises(error, match=str(status_code)):
        if asynchronous:
            await extractor.aextract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )
        else:
            extractor.extract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_network_errors_are_mapped_to_retryable_document_errors(
    document_path: Path, asynchronous: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection lost", request=request)

    extractor = _extractor(handler)

    with pytest.raises(RetryableDocumentUpstreamError, match="网络"):
        if asynchronous:
            await extractor.aextract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )
        else:
            extractor.extract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )


@pytest.mark.parametrize(
    ("failure_type", "message"),
    [
        (httpx.ConnectTimeout, "连接超时"),
        (httpx.ReadTimeout, "单次请求超时"),
    ],
)
def test_request_timeout_categories_are_preserved(
    document_path: Path,
    failure_type: type[httpx.TimeoutException],
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure_type("slow request", request=request)

    extractor = _extractor(handler)

    with pytest.raises(RetryableDocumentUpstreamError, match=message):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


def test_upload_timeout_is_distinct_from_response_timeout(document_path: Path) -> None:
    health = {
        "status": "healthy",
        "version": "3.3.0",
        "protocol_version": MINERU_API_PROTOCOL_VERSION,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json=health)
        raise httpx.WriteTimeout("slow upload", request=request)

    extractor = _extractor(handler)

    with pytest.raises(RetryableDocumentUpstreamError, match="上传超时"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_missing_remote_task_is_resubmitted_only_once(
    document_path: Path, asynchronous: bool
) -> None:
    server = ScriptedMinerU(
        statuses=("completed",),
        missing_task_ids={"task-1"},
    )
    extractor = _extractor(server)

    if asynchronous:
        result = await extractor.aextract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )
    else:
        result = extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )

    assert result.markdown == MARKDOWN
    assert server.submissions == 2


def test_second_missing_remote_task_is_retryable_without_third_submission(
    document_path: Path,
) -> None:
    server = ScriptedMinerU(
        statuses=("completed",),
        missing_task_ids={"task-1", "task-2"},
    )
    extractor = _extractor(server)

    with pytest.raises(RetryableDocumentUpstreamError, match="丢失"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )

    assert server.submissions == 2


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_total_parse_deadline_uses_injected_clock_without_real_sleep(
    document_path: Path, asynchronous: bool
) -> None:
    clock = FakeClock()
    server = ScriptedMinerU(statuses=("processing",))
    extractor = _extractor(
        server,
        clock=clock,
        sleep=clock.sleep,
        asleep=clock.asleep,
        parse_timeout_seconds=0.5,
        poll_interval_seconds=0.25,
    )

    with pytest.raises(RetryableDocumentUpstreamError, match="总解析超时"):
        if asynchronous:
            await extractor.aextract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )
        else:
            extractor.extract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )

    assert clock.now == 0.5


def test_response_body_limit_is_checked_before_json_decode(document_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 9)

    extractor = _extractor(handler, max_response_bytes=8)

    with pytest.raises(DocumentResourceLimitExceeded, match="响应体"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


class TrackingSyncStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        yield b"x" * 9

    def close(self) -> None:
        self.closed = True


def test_streamed_response_limit_closes_body_without_buffering_oversized_chunk(
    document_path: Path,
) -> None:
    stream = TrackingSyncStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    extractor = _extractor(handler, max_response_bytes=8)

    with pytest.raises(DocumentResourceLimitExceeded, match="响应体"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )

    assert stream.closed


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_markdown_utf8_size_has_an_independent_limit(
    document_path: Path, asynchronous: bool
) -> None:
    server = ScriptedMinerU(
        statuses=("completed",),
        result={"results": {"sample": {"md_content": "中文"}}},
    )
    extractor = _extractor(server, max_markdown_bytes=5)

    with pytest.raises(DocumentResourceLimitExceeded, match="Markdown"):
        if asynchronous:
            await extractor.aextract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )
        else:
            extractor.extract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )


def test_phase_specific_timeouts_are_sent_to_httpx(document_path: Path) -> None:
    server = ScriptedMinerU(statuses=("completed",))
    extractor = _extractor(
        server,
        clock=lambda: 0.0,
        connect_timeout_seconds=3.0,
        request_timeout_seconds=7.0,
        upload_timeout_seconds=11.0,
        parse_timeout_seconds=13.0,
    )

    extractor.extract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )

    health_timeout = server.requests[0].extensions["timeout"]
    upload_timeout = server.requests[1].extensions["timeout"]
    assert health_timeout["connect"] == 3.0
    assert health_timeout["read"] == 7.0
    assert health_timeout["write"] == 7.0
    assert upload_timeout["connect"] == 3.0
    assert upload_timeout["read"] == 7.0
    assert upload_timeout["write"] == 11.0


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_slow_upload_is_stopped_by_total_deadline(
    document_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
) -> None:
    payload = document_path.read_bytes()
    clock = FakeClock()
    original_open = Path.open

    class SlowReader(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            clock.now += 0.6
            return super().read(size)

    def open_with_slow_pdf(self: Path, *args: Any, **kwargs: Any):
        if self == document_path:
            return SlowReader(payload)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_slow_pdf)
    extractor = _extractor(
        ScriptedMinerU(statuses=("completed",)),
        clock=clock,
        parse_timeout_seconds=0.5,
    )

    with pytest.raises(RetryableDocumentUpstreamError, match="总解析超时"):
        if asynchronous:
            await extractor.aextract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )
        else:
            extractor.extract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )


class SlowSyncStream(httpx.SyncByteStream):
    def __init__(self, payload: bytes, clock: FakeClock) -> None:
        self.payload = payload
        self.clock = clock
        self.closed = False

    def __iter__(self):
        self.clock.now += 0.6
        yield self.payload

    def close(self) -> None:
        self.closed = True


class SlowAsyncStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes, clock: FakeClock) -> None:
        self.payload = payload
        self.clock = clock
        self.closed = False

    async def __aiter__(self):
        self.clock.now += 0.6
        yield self.payload

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_slow_response_is_stopped_and_closed_at_total_deadline(
    document_path: Path, asynchronous: bool
) -> None:
    clock = FakeClock()
    result = json.dumps({"results": {"sample": {"md_content": MARKDOWN}}}).encode()
    stream = (
        SlowAsyncStream(result, clock)
        if asynchronous
        else SlowSyncStream(result, clock)
    )
    server = ScriptedMinerU(statuses=("completed",))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            return httpx.Response(200, stream=stream)
        return server(request)

    extractor = _extractor(
        handler,
        clock=clock,
        parse_timeout_seconds=0.5,
    )

    with pytest.raises(RetryableDocumentUpstreamError, match="总解析超时"):
        if asynchronous:
            await extractor.aextract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )
        else:
            extractor.extract(
                document_path, filename="sample.pdf", media_type="application/pdf"
            )

    assert stream.closed


class BlockingAsyncStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed.set()


class BlockingUploadTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.upload_started = asyncio.Event()
        self.upload_cancelled = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.3.0",
                    "protocol_version": MINERU_API_PROTOCOL_VERSION,
                },
            )
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            stream = request.stream
            assert isinstance(stream, httpx.AsyncByteStream)
            async for _chunk in stream:
                self.upload_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.upload_cancelled.set()
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


async def test_async_total_deadline_cancels_blocked_upload(
    document_path: Path,
) -> None:
    transport = BlockingUploadTransport()
    client = httpx.AsyncClient(transport=transport)
    extractor = MinerUDocumentExtractor(
        BASE_URL,
        async_client=client,
        parse_timeout_seconds=0.02,
    )

    with pytest.raises(RetryableDocumentUpstreamError, match="总解析超时"):
        await extractor.aextract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )

    assert transport.upload_started.is_set()
    assert transport.upload_cancelled.is_set()
    await client.aclose()


async def test_async_total_deadline_cancels_blocked_response(
    document_path: Path,
) -> None:
    stream = BlockingAsyncStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    extractor = _extractor(handler, parse_timeout_seconds=0.02)

    with pytest.raises(RetryableDocumentUpstreamError, match="总解析超时"):
        await extractor.aextract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )

    assert stream.started.is_set()
    assert stream.closed.is_set()


async def test_cancellation_closes_the_active_response_stream(
    document_path: Path,
) -> None:
    stream = BlockingAsyncStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    extractor = _extractor(handler)
    task = asyncio.create_task(
        extractor.aextract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )
    )
    await stream.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed.is_set()


async def test_extractor_gate_limits_whole_async_parse_lifecycle(
    document_path: Path,
) -> None:
    active = 0
    peak = 0
    submissions = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak, submissions
        path = request.url.path
        if path.endswith("/health"):
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            await release.wait()
            return httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "version": "3.3.0",
                    "protocol_version": MINERU_API_PROTOCOL_VERSION,
                },
            )
        if request.method == "POST" and path.endswith("/tasks"):
            submissions += 1
            return httpx.Response(202, json={"task_id": f"task-{submissions}"})
        if path.endswith("/result"):
            active -= 1
            return httpx.Response(
                200,
                json={"results": {"sample": {"md_content": MARKDOWN}}},
            )
        task_id = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"task_id": task_id, "status": "completed"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = MinerUDocumentExtractor(BASE_URL, async_client=client)
    gate = Gate(limit=2, max_waiting=5, acquire_timeout=1.0, name="mineru")
    extractor.bind_gate(gate)
    tasks = [
        asyncio.create_task(
            extractor.aextract(
                document_path,
                filename=f"sample-{index}.pdf",
                media_type="application/pdf",
            )
        )
        for index in range(5)
    ]
    await two_started.wait()

    assert active == 2
    assert gate.stats.in_flight == 2
    release.set()
    results = await asyncio.gather(*tasks)

    assert [result.markdown for result in results] == [MARKDOWN] * 5
    assert peak == 2
    assert gate.stats.peak_in_flight == 2
    assert gate.stats.in_flight == 0
    await extractor.aclose()
    await client.aclose()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_url": "mineru.test"}, "HTTP"),
        ({"base_url": BASE_URL, "parse_method": "magic"}, "parse_method"),
        ({"base_url": BASE_URL, "poll_interval_seconds": -1}, "poll_interval"),
        ({"base_url": BASE_URL, "connect_timeout_seconds": 0}, "connect_timeout"),
        ({"base_url": BASE_URL, "request_timeout_seconds": 0}, "request_timeout"),
        ({"base_url": BASE_URL, "upload_timeout_seconds": 0}, "upload_timeout"),
        ({"base_url": BASE_URL, "parse_timeout_seconds": 0}, "parse_timeout"),
        ({"base_url": BASE_URL, "max_response_bytes": 0}, "max_response"),
        ({"base_url": BASE_URL, "max_markdown_bytes": 0}, "max_markdown"),
    ],
)
def test_constructor_rejects_invalid_wire_configuration(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MinerUDocumentExtractor(**kwargs)


def test_constructor_rejects_remote_cleartext_endpoint() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        MinerUDocumentExtractor("http://mineru.internal:8989")
