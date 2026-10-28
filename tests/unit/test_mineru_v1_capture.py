from __future__ import annotations

import asyncio
import json
import threading
from argparse import Namespace
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.extractors._mineru_v1_files import CaptureFiles
from comet_rag.infrastructure.extractors._mineru_v1_protocol import (
    CaptureError,
    CaptureLimits,
    CaptureSource,
)
from comet_rag.infrastructure.extractors.mineru_v1 import AsyncMinerUV1Collector
from evals.pdf.capture import main_async


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.closed = False
        self.iterated = False

    async def __aiter__(self):
        self.iterated = True
        yield self.data

    async def aclose(self) -> None:
        self.closed = True


def response(
    value: object, *, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return httpx.Response(
        status,
        headers={
            "content-type": "application/json",
            "content-length": str(len(raw)),
            **(headers or {}),
        },
        stream=Chunks(raw),
    )


def artifact() -> bytes:
    return json.dumps(
        {
            "metadata": {
                "file_suffix": "pdf",
                "producer": {"name": "mineru", "version": "4.0.12"},
                "document": {"page_count": 1, "page_count_kind": "physical"},
            },
            "is_full_document": True,
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {"type": "text", "content": "Source text", "bbox": [0, 0, 1, 1]}
                    ],
                }
            ],
        }
    ).encode()


@pytest.fixture
def source(tmp_path: Path) -> CaptureSource:
    path = tmp_path / "input.pdf"
    path.write_bytes(b"%PDF-1.7\nunit fixture - never sent to a real parser")
    return CaptureSource(
        path,
        "sample.pdf",
        sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
        1,
    )


class Server:
    """手写协议替身；任务和文件期望不从被测解析器推导。"""

    def __init__(self, source: CaptureSource, label: str = "1") -> None:
        self.source = source
        self.job_id = "job_" + label
        self.source_id = "source_" + label
        self.output_id = "output_" + label
        self.data = artifact()
        self.calls: list[tuple[str, str]] = []
        self.posts = 0
        self.deletes: list[str] = []
        self.poll_status = "completed"
        self.mutate: Callable[[httpx.Request, dict[str, Any]], dict[str, Any]] = (
            lambda _, value: value
        )
        # Any仅用于制造错误JSON形态，正式协议仍有严格类型校验。
        self.special: Callable[[httpx.Request], httpx.Response | None] = lambda _: None
        self.uploaded = b""
        self.headers: dict[str, str] = {}

    def frame(self, state: str) -> dict[str, Any]:
        completed = state == "completed"
        return {
            "task_id": self.job_id,
            "status": state,
            "tier": "advanced",
            "output_formats": ["structured_content"],
            "status_url": "/v1/tasks/" + self.job_id,
            "result_url": "/v1/tasks/" + self.job_id + "/result",
            "links": {
                "self": "/v1/tasks/" + self.job_id,
                "cancel": "/v1/tasks/" + self.job_id,
            },
            "progress": {"total": 1, "completed": int(completed), "failed": 0},
            "files": [
                {
                    "file_id": self.source_id,
                    "name": self.source.filename,
                    "status": state,
                    "page_range": "1" if completed else "all",
                    "parse": {"parser_version": "4.0.12"} if completed else None,
                    "output_files": {
                        "structured_content": {
                            "file_id": self.output_id,
                            "bytes": len(self.data),
                        },
                        "markdown": None,
                    }
                    if completed
                    else None,
                }
            ],
        }

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        assert request.url.host == "localhost"
        assert request.headers["accept-encoding"] == "identity"
        path = request.url.path
        if request.method == "POST":
            self.posts += 1
            self.uploaded = await request.aread()
            assert request.url.params["page_range"] == "all"
            assert request.url.params["output_formats"] == "structured_content"
            assert "sha256sum" not in request.url.params
            assert int(request.headers["content-length"]) == self.source.size
        special = self.special(request)
        if special is not None:
            return special
        if path == "/v1/health":
            value = {
                "status": "ok",
                "version": "4.0.12",
                "features": {"output_formats": ["structured_content"]},
            }
        elif path == "/v1/tiers":
            value = {"object": "list", "data": [{"id": "advanced"}]}
        elif request.method == "POST":
            value = self.frame("queued")
        elif request.method == "DELETE":
            self.deletes.append(path)
            value = self.frame("canceled")
        elif path == "/v1/tasks/" + self.job_id:
            value = self.frame(self.poll_status)
        elif path == "/v1/files/" + self.source_id:
            value = {
                "id": self.source_id,
                "object": "file",
                "filename": self.source.filename,
                "purpose": "parse",
                "bytes": self.source.size,
                "sha256sum": self.source.sha256,
            }
        elif path == "/v1/files/" + self.output_id:
            value = {
                "id": self.output_id,
                "object": "file",
                "filename": self.source.filename + ".structured_content.json",
                "purpose": "parse_output",
                "bytes": len(self.data),
                "sha256sum": sha256(self.data).hexdigest(),
            }
        elif path.endswith("/content"):
            return response(
                self.data,
                headers={"content-type": "application/octet-stream", **self.headers},
            )
        else:
            raise AssertionError((request.method, path))
        return response(
            self.mutate(request, value), status=202 if request.method == "POST" else 200
        )


def limits(**kwargs) -> CaptureLimits:
    # 快速轮询只为避免单测固定等待；真正超时测试另用事件或时钟故障。
    return replace(CaptureLimits(), poll_seconds=0.001, **kwargs)


def receipt(output: Path) -> dict:
    return json.loads((output / "receipt.json").read_text())


async def test_collects_only_native_structured_and_leaves_borrowed_client_open(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    gate = Gate(limit=1, max_waiting=1)
    out = tmp_path / "capture"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), follow_redirects=True
    ) as client:
        result = await AsyncMinerUV1Collector(
            "http://localhost", client=client, gate=gate, limits=limits()
        ).collect(source, out)
        assert not client.is_closed
    assert result["outcome"] == "completed"
    assert result["source_verified"] is True
    assert result["submission"] == "source_verified"
    assert server.posts == 1 and server.deletes == []
    assert server.uploaded == source.path.read_bytes()
    assert (out / "structured_content.json").read_bytes() == server.data
    assert receipt(out)["artifact"]["sha256"] == sha256(server.data).hexdigest()
    assert not (out / ".structured_content.part").exists()
    assert gate.stats.in_flight == 0


@pytest.mark.parametrize("kind", ["empty", "interpretation_only"])
async def test_completed_without_extracted_text_cannot_be_published(
    source: CaptureSource, tmp_path: Path, kind: str
) -> None:
    server = Server(source)
    data = json.loads(server.data)
    data["pages"][0]["blocks"] = (
        []
        if kind == "empty"
        else [
            {"type": "image", "content": "Model interpretation", "bbox": [0, 0, 1, 1]}
        ]
    )
    server.data = json.dumps(data).encode()
    gate = Gate(limit=1, max_waiting=1)
    out = tmp_path / "empty-source"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError, match="invalid_structured_artifact"):
            await AsyncMinerUV1Collector(
                "http://localhost", client=client, gate=gate, limits=limits()
            ).collect(source, out)
    assert receipt(out)["outcome"] == "failed"
    assert receipt(out)["artifact"] == {}
    assert not (out / "structured_content.json").exists()
    assert server.posts == 1
    assert gate.stats.in_flight == 0


async def test_raw_upload_avoids_early_sha_cache_response_but_verifies_file_hash(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)

    def special(request):
        if request.method == "POST" and "sha256sum" in request.url.params:
            raise httpx.ReadError("upstream cache hit returned before upload completed")
        return None

    server.special = special
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        await AsyncMinerUV1Collector(
            "http://localhost",
            client=client,
            gate=Gate(limit=1, max_waiting=1),
            limits=limits(),
        ).collect(source, out)
    assert server.posts == 1 and server.uploaded == source.path.read_bytes()
    assert ("GET", "/v1/files/source_1") in server.calls
    assert receipt(out)["source_verified"] is True


@pytest.mark.parametrize(
    "fault", ["transport", "truncated_json", "unsafe_id", "oversized_reply"]
)
async def test_lost_or_untrusted_submit_reply_is_never_resubmitted(
    source: CaptureSource, tmp_path: Path, fault: str
) -> None:
    server = Server(source)

    def special(request: httpx.Request):
        if request.method != "POST":
            return None
        if fault == "transport":
            raise httpx.ReadError("response lost after server created job")
        if fault == "truncated_json":
            return response(b'{"task_id":', status=202)
        if fault == "unsafe_id":
            return response({"task_id": "../some-other-job"}, status=202)
        return response(b"x" * 4097, status=202)

    server.special = special
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        collector = AsyncMinerUV1Collector(
            "http://localhost",
            client=client,
            gate=Gate(limit=1, max_waiting=1),
            limits=limits(control_bytes=4096),
        )
        with pytest.raises(CaptureError, match="submission_unknown"):
            await collector.collect(source, out)
    assert server.posts == 1 and server.deletes == []
    assert receipt(out)["submission"] == "unknown"
    assert receipt(out)["task_id"] is None
    assert receipt(out)["outcome"] != "completed"
    assert not (out / "structured_content.json").exists()


@pytest.mark.parametrize("fault", ["late_deadline", "publish_failure"])
async def test_publication_is_rolled_back_if_final_deadline_or_receipt_write_fails(
    source: CaptureSource, tmp_path: Path, monkeypatch, fault: str
) -> None:
    server, clock = Server(source), Clock()
    original = CaptureFiles.publish

    def publish(files, value, check):
        if fault == "publish_failure":
            # 模拟原子重命名已经完成，而成功回执尚未落盘。
            files.part.replace(files.output / "structured_content.json")
            raise OSError("injected publication failure")
        original(files, value, check)
        clock.value = 108.1

    monkeypatch.setattr(CaptureFiles, "publish", publish)
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(total_seconds=10, cancel_seconds=2),
                clock=clock,
            ).collect(source, out)
    assert receipt(out)["outcome"] != "completed"
    assert receipt(out)["artifact"] == {}
    assert not (out / "structured_content.json").exists()


@pytest.mark.parametrize(
    "fault", ["wrong_source_hash", "wrong_name", "wrong_source_id", "evil_link"]
)
async def test_unverified_source_never_authorizes_cancel(
    source: CaptureSource, tmp_path: Path, fault: str
) -> None:
    server = Server(source)

    def mutate(request, value):
        if fault == "wrong_source_hash" and request.url.path == "/v1/files/source_1":
            value["sha256sum"] = "0" * 64
        if request.method == "POST":
            if fault == "wrong_name":
                value["files"][0]["name"] = "another.pdf"
            elif fault == "wrong_source_id":
                value["files"][0]["file_id"] = "https://elsewhere.invalid"
            elif fault == "evil_link":
                value["status_url"] = "https://elsewhere.invalid/tasks/secret"
        return value

    server.mutate = mutate
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(),
            ).collect(source, out)
    assert receipt(out)["task_id"] == "job_1"
    assert receipt(out)["cancellation"] == "skipped_unverified_source"
    assert server.deletes == []


async def test_task_id_receipt_precedes_source_check_and_polling(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    out = tmp_path / "capture"

    def special(request):
        if request.url.path == "/v1/files/source_1":
            saved = receipt(out)
            assert (
                saved["task_id"] == "job_1" and saved["submission"] == "task_observed"
            )
        return None

    server.special = special
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        await AsyncMinerUV1Collector(
            "http://localhost",
            client=client,
            gate=Gate(limit=1, max_waiting=1),
            limits=limits(),
        ).collect(source, out)


@pytest.mark.parametrize(
    "fault",
    [
        "failed",
        "partial",
        "canceled",
        "invented",
        "wrong_id",
        "bool_progress",
        "wrong_output",
        "bool_size",
    ],
)
async def test_invalid_task_result_never_publishes_artifact(
    source: CaptureSource, tmp_path: Path, fault: str
) -> None:
    server = Server(source)
    if fault in {"failed", "partial", "canceled", "invented"}:
        server.poll_status = fault

    def mutate(request, value):
        if request.method == "GET" and request.url.path == "/v1/tasks/job_1":
            if fault == "wrong_id":
                value["task_id"] = "job_someone_else"
            elif fault == "bool_progress":
                value["progress"]["completed"] = True
            elif fault == "wrong_output":
                value["output_formats"] = ["markdown"]
            elif fault == "bool_size":
                value["files"][0]["output_files"]["structured_content"]["bytes"] = True
        return value

    server.mutate = mutate
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(),
            ).collect(source, out)
    assert not (out / "structured_content.json").exists()
    assert receipt(out)["outcome"] != "completed"
    assert all(path == "/v1/tasks/job_1" for path in server.deletes)


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_digest",
        "wrong_size",
        "bad_json",
        "wrong_version",
        "missing_page",
        "no_text",
        "wrong_media",
        "gzip",
        "length_lie",
    ],
)
async def test_bad_artifact_is_discarded_after_completed_remote_job(
    source: CaptureSource, tmp_path: Path, fault: str
) -> None:
    server = Server(source)
    if fault == "bad_json":
        server.data = b"not JSON"
    elif fault in {"wrong_version", "missing_page", "no_text"}:
        value = json.loads(server.data)
        if fault == "wrong_version":
            value["metadata"]["producer"]["version"] = "4.0.13"
        elif fault == "missing_page":
            value["pages"] = []
        else:
            value["pages"][0]["blocks"] = []
        server.data = json.dumps(value).encode()
    if fault == "wrong_media":
        server.headers["content-type"] = "text/html"
    elif fault == "gzip":
        server.headers["content-encoding"] = "gzip"
    elif fault == "length_lie":
        server.headers["content-length"] = "1"

    def mutate(request, value):
        if request.url.path == "/v1/files/output_1":
            if fault == "wrong_digest":
                value["sha256sum"] = "0" * 64
            elif fault == "wrong_size":
                value["bytes"] += 1
        return value

    server.mutate = mutate
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(),
            ).collect(source, out)
    assert server.deletes == []
    assert receipt(out)["remote_status"] == "completed"
    assert receipt(out)["outcome"] == "failed"
    assert not (out / "structured_content.json").exists()
    assert not (out / ".structured_content.part").exists()


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


@pytest.mark.parametrize("exhausted", [False, True])
async def test_total_deadline_never_resets_and_cancel_uses_only_remaining_reserve(
    source: CaptureSource, tmp_path: Path, exhausted: bool
) -> None:
    server, clock = Server(source), Clock()

    def special(request):
        if request.method == "GET" and request.url.path == "/v1/tasks/job_1":
            clock.value = 111.0 if exhausted else 108.1
        return None

    server.special = special
    out = tmp_path / "capture"
    gate = Gate(limit=1, max_waiting=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError, match="deadline_exceeded"):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=gate,
                clock=clock,
                limits=limits(total_seconds=10, cancel_seconds=2),
            ).collect(source, out)
    saved = receipt(out)
    assert saved["outcome"] == "timed_out"
    assert saved["cancellation"] == ("unconfirmed" if exhausted else "acknowledged")
    assert len(server.deletes) == (0 if exhausted else 1)
    assert saved["remote_drained"] is False
    assert gate.stats.in_flight == 0


async def test_request_phase_timeout_cancels_known_task_inside_total_budget(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    pending = asyncio.Event()

    async def handler(request):
        if request.method == "GET" and request.url.path == "/v1/tasks/job_1":
            await pending.wait()
        return await server(request)

    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CaptureError, match="request_timeout"):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(request_seconds=0.03),
            ).collect(source, out)
    assert server.deletes == ["/v1/tasks/job_1"]
    assert receipt(out)["cancellation"] == "acknowledged"


async def test_repeated_caller_cancel_holds_gate_until_bounded_cleanup_finishes(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    poll_started, delete_started, release_delete = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def handler(request):
        if request.method == "GET" and request.url.path == "/v1/tasks/job_1":
            poll_started.set()
            await asyncio.Event().wait()
        if request.method == "DELETE":
            delete_started.set()
            await release_delete.wait()
        return await server(request)

    gate = Gate(limit=1, max_waiting=1)
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            AsyncMinerUV1Collector(
                "http://localhost", client=client, gate=gate, limits=limits()
            ).collect(source, out)
        )
        async with asyncio.timeout(2):
            await poll_started.wait()
            task.cancel()
            await delete_started.wait()
            task.cancel()
            assert gate.stats.in_flight == 1
            release_delete.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert gate.stats.in_flight == 0
    assert receipt(out)["outcome"] == "cancelled"
    assert receipt(out)["cancellation"] == "acknowledged"


async def test_queue_wait_is_inside_deadline_and_does_not_submit(
    source: CaptureSource, tmp_path: Path
) -> None:
    server, gate = Server(source), Gate(limit=1, max_waiting=1)
    await gate.acquire()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            with pytest.raises(CaptureError, match="gate_wait_timeout"):
                await AsyncMinerUV1Collector(
                    "http://localhost",
                    client=client,
                    gate=gate,
                    limits=limits(gate_seconds=0.02),
                ).collect(source, tmp_path / "capture")
        assert gate.stats.waiting == 0 and gate.stats.in_flight == 1
        assert server.posts == 0
    finally:
        gate.release()


async def test_full_gate_is_explicitly_rejected_without_network(
    source: CaptureSource, tmp_path: Path
) -> None:
    server, gate = Server(source), Gate(limit=1, max_waiting=1)
    await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    try:
        async with asyncio.timeout(1):
            while gate.stats.waiting != 1:
                await asyncio.sleep(0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            with pytest.raises(CaptureError, match="gate_overloaded"):
                await AsyncMinerUV1Collector(
                    "http://localhost", client=client, gate=gate, limits=limits()
                ).collect(source, tmp_path / "capture")
        assert server.calls == []
        assert receipt(tmp_path / "capture")["error"] == "gate_overloaded"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        gate.release()


async def test_cumulative_budget_limits_polling_and_cancel_cannot_bypass_it(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    server.poll_status = "queued"
    polls = 0

    def stop_broken_loop(request):
        nonlocal polls
        if request.method == "GET" and request.url.path == "/v1/tasks/job_1":
            polls += 1
            if polls > 10:
                raise AssertionError("缺陷注入移除预算时须立即使测试失败，不能等900秒")
        return None

    server.special = stop_broken_loop
    out = tmp_path / "capture"
    selected = limits(control_bytes=2048, artifact_bytes=1024, received_bytes=4096)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError, match="receive_limit"):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=selected,
            ).collect(source, out)
    assert receipt(out)["received_bytes"] <= 4096
    assert server.posts == 1
    assert len(server.calls) < 20


async def test_existing_output_and_wrong_local_source_never_send_network(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    out = tmp_path / "capture"
    out.mkdir()
    marker = out / "receipt.json"
    marker.write_text("user-owned")
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        collector = AsyncMinerUV1Collector(
            "http://localhost",
            client=client,
            gate=Gate(limit=1, max_waiting=1),
            limits=limits(),
        )
        with pytest.raises(CaptureError):
            await collector.collect(source, out)
        assert marker.read_text() == "user-owned"
        with pytest.raises(CaptureError, match="source_digest_mismatch"):
            await collector.collect(
                replace(source, sha256="0" * 64), tmp_path / "wrong-source"
            )
    assert server.calls == []


async def test_receipt_write_failure_after_observing_id_stops_before_poll(
    source: CaptureSource, tmp_path: Path, monkeypatch
) -> None:
    server = Server(source)
    original = CaptureFiles.save_receipt

    def fail(files, value):
        if value["task_id"] is not None:
            raise OSError("injected disk failure")
        return original(files, value)

    monkeypatch.setattr(CaptureFiles, "save_receipt", fail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError) as caught:
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(),
            ).collect(source, tmp_path / "capture")
    assert caught.value.task_id == "job_1"
    assert ("GET", "/v1/files/source_1") not in server.calls
    assert ("GET", "/v1/tasks/job_1") not in server.calls
    assert server.posts == 1 and server.deletes == []


@pytest.mark.parametrize("fault", ["version", "tier", "redirect"])
async def test_capability_preflight_rejects_before_submission(
    source: CaptureSource, tmp_path: Path, fault: str
) -> None:
    server = Server(source)

    def special(request):
        if fault == "redirect" and request.url.path == "/v1/health":
            return response(
                {}, status=302, headers={"location": "https://elsewhere.invalid/steal"}
            )
        return None

    def mutate(request, value):
        if fault == "version" and request.url.path == "/v1/health":
            value["version"] = "4.0.13"
        if fault == "tier" and request.url.path == "/v1/tiers":
            value["data"] = [{"id": "basic"}]
        return value

    server.special, server.mutate = special, mutate
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), follow_redirects=True
    ) as client:
        with pytest.raises(CaptureError):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(),
            ).collect(source, tmp_path / "capture")
    assert server.posts == 0
    assert all(path in {"/v1/health", "/v1/tiers"} for _, path in server.calls)


async def test_actual_stream_limit_is_enforced_without_content_length_and_stream_closes(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    stream = Chunks(b"x" * 65)
    server.special = lambda request: (
        httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
        if request.url.path == "/v1/health"
        else None
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError, match="response_receive_limit"):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(control_bytes=64),
            ).collect(source, tmp_path / "capture")
    assert stream.iterated and stream.closed
    assert server.posts == 0


async def test_gzip_is_rejected_before_decompression_or_body_iteration(
    source: CaptureSource, tmp_path: Path
) -> None:
    server = Server(source)
    stream = Chunks(b"this need not even be a valid gzip stream")
    server.special = lambda request: (
        httpx.Response(
            200,
            headers={
                "content-type": "application/octet-stream",
                "content-encoding": "gzip",
            },
            stream=stream,
        )
        if request.url.path.endswith("/content")
        else None
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError, match="unexpected_content_encoding"):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(),
            ).collect(source, tmp_path / "capture")
    assert stream.closed and not stream.iterated


@pytest.mark.parametrize(
    "fault", ["already_completed", "wrong_id", "not_terminal", "lost_reply"]
)
async def test_cancel_reply_does_not_prove_remote_drain(
    source: CaptureSource, tmp_path: Path, fault: str
) -> None:
    server, clock = Server(source), Clock()

    def special(request):
        if request.method == "GET" and request.url.path == "/v1/tasks/job_1":
            clock.value = 108.1
        if request.method == "DELETE":
            if fault == "lost_reply":
                raise httpx.ReadError("cancel may have committed")
            return response(
                {
                    "task_id": "job_other" if fault == "wrong_id" else "job_1",
                    "status": "completed"
                    if fault == "already_completed"
                    else "running",
                }
            )
        return None

    server.special = special
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError, match="deadline_exceeded"):
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(total_seconds=10, cancel_seconds=2),
                clock=clock,
            ).collect(source, out)
    saved = receipt(out)
    assert saved["cancellation"] == (
        "remote_terminal" if fault == "already_completed" else "unconfirmed"
    )
    assert saved["remote_drained"] is False and saved["outcome"] == "timed_out"


async def test_cancel_during_disk_write_drains_thread_before_closing_file_or_gate(
    source: CaptureSource, tmp_path: Path, monkeypatch
) -> None:
    server, gate = Server(source), Gate(limit=1, max_waiting=1)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    handles = []
    original = CaptureFiles.write_artifact

    def blocked(files, chunk):
        handles.append(files.artifact)
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(2):
            raise AssertionError("test failed to release writer")
        original(files, chunk)

    monkeypatch.setattr(CaptureFiles, "write_artifact", blocked)
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        task = asyncio.create_task(
            AsyncMinerUV1Collector(
                "http://localhost", client=client, gate=gate, limits=limits()
            ).collect(source, out)
        )
        try:
            async with asyncio.timeout(2):
                await entered.wait()
                task.cancel()
                await asyncio.sleep(0)
                assert gate.stats.in_flight == 1 and not handles[0].closed
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.is_closed
    assert handles[0].closed and gate.stats.in_flight == 0
    assert not (out / ".structured_content.part").exists()


@pytest.mark.parametrize("bad_artifact", [False, True])
async def test_local_close_failure_never_publishes_success_or_masks_primary_error(
    source: CaptureSource, tmp_path: Path, monkeypatch, bad_artifact: bool
) -> None:
    server = Server(source)
    if bad_artifact:
        server.data = b"invalid JSON"
    original = CaptureFiles.close
    injected = False

    def fail_once(files):
        nonlocal injected
        original(files)
        if not injected:
            injected = True
            raise OSError("injected local close failure")

    monkeypatch.setattr(CaptureFiles, "close", fail_once)
    out = tmp_path / "capture"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(CaptureError) as caught:
            await AsyncMinerUV1Collector(
                "http://localhost",
                client=client,
                gate=Gate(limit=1, max_waiting=1),
                limits=limits(),
            ).collect(source, out)
    assert caught.value.task_id == "job_1"
    if bad_artifact:
        assert caught.value.code == "invalid_structured_artifact"
    assert receipt(out)["outcome"] != "completed"
    assert not (out / "structured_content.json").exists()


async def test_shared_collector_keeps_concurrent_receipts_and_artifacts_isolated(
    source: CaptureSource, tmp_path: Path
) -> None:
    sources = {
        name: replace(source, filename=name + ".pdf") for name in ("first", "second")
    }
    servers = {name: Server(value, name) for name, value in sources.items()}
    for name, server in servers.items():
        value = json.loads(server.data)
        value["pages"][0]["blocks"][0]["content"] = name
        server.data = json.dumps(value).encode()
    submitted = asyncio.Event()
    posts = 0

    async def handler(request):
        nonlocal posts
        path = request.url.path
        if path in {"/v1/health", "/v1/tiers"}:
            return await servers["first"](request)
        name = (
            Path(request.url.params["filename"]).stem
            if request.method == "POST"
            else path.rsplit("_", 1)[1].split("/", 1)[0]
        )
        if request.method == "POST":
            posts += 1
            if posts == 2:
                submitted.set()
        elif path.startswith("/v1/tasks/"):
            await submitted.wait()
        return await servers[name](request)

    gate = Gate(limit=2, max_waiting=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = AsyncMinerUV1Collector(
            "http://localhost", client=client, gate=gate, limits=limits()
        )
        async with asyncio.timeout(2):
            await asyncio.gather(
                *(
                    collector.collect(value, tmp_path / name)
                    for name, value in sources.items()
                )
            )
        assert not client.is_closed
    for name in sources:
        assert receipt(tmp_path / name)["task_id"] == "job_" + name
        value = json.loads((tmp_path / name / "structured_content.json").read_text())
        assert value["pages"][0]["blocks"][0]["content"] == name
    assert gate.stats.in_flight == 0 and gate.stats.peak_in_flight == 2


@pytest.mark.parametrize("missing", ["sha256", "bytes", "pages"])
async def test_cli_rejects_incomplete_source_lock_before_network(
    tmp_path: Path, missing: str
) -> None:
    sample: dict[str, object] = {
        "id": "sample",
        "path": str(tmp_path / "source.pdf"),
        "sha256": "a" * 64,
        "bytes": 100,
        "pages": 1,
    }
    del sample[missing]
    lock = tmp_path / "inputs.json"
    lock.write_text(json.dumps({"samples": [sample]}))
    # 不完整身份先于客户端构造失败；不需要真实PDF或可连接的端点。
    with pytest.raises(CaptureError, match="protocol_digest|protocol_integer"):
        await main_async(
            Namespace(
                source_lock=lock,
                sample="sample",
                api_key_env=None,
                url="not-an-endpoint",
                out=tmp_path / "out",
            )
        )
    assert not (tmp_path / "out").exists()
