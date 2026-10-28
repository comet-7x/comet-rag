from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

import httpx

from comet_rag.core.concurrency import Gate, Overloaded
from comet_rag.core.http_endpoint import validate_http_endpoint
from comet_rag.engines.documents.mineru.structured import (
    DEFAULT_LIMITS,
    decode_json,
    project_structured,
)
from comet_rag.infrastructure.evidence.files import read_limited
from comet_rag.infrastructure.extractors._mineru_v1_files import (
    CHUNK_BYTES,
    CaptureFiles,
)
from comet_rag.infrastructure.extractors._mineru_v1_protocol import (
    FORMAT,
    RUNNING,
    TERMINAL,
    CaptureError,
    CaptureLimits,
    CaptureSource,
    TaskFrame,
    check_health,
    check_tiers,
    file_digest,
    identifier,
    task_frame,
)
from comet_rag.infrastructure.sources.base import finish

__all__ = [
    "AsyncMinerUV1Collector",
    "CaptureError",
    "CaptureLimits",
    "CaptureSource",
]


async def _blocking[T, **P](
    call: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    return await finish(asyncio.create_task(asyncio.to_thread(call, *args, **kwargs)))


@dataclass(slots=True)
class _Run:
    source: CaptureSource
    files: CaptureFiles
    limits: CaptureLimits
    endpoint_sha256: str
    end: float
    clock: Callable[[], float]
    received: int = 0
    task_id: str | None = None
    source_file_id: str | None = None
    source_verified: bool = False
    remote_status: str | None = None
    submission: str = "not_sent"
    outcome: str = "preparing"
    cancellation: str = "not_requested"
    error: str | None = None
    artifact: dict[str, object] = field(default_factory=dict)
    cleanup_errors: list[str] = field(default_factory=list)

    def remaining(self, *, cleanup: bool = False) -> float:
        end = self.end if cleanup else self.end - self.limits.cancel_seconds
        remaining = end - self.clock()
        if remaining <= 0:
            raise CaptureError("deadline_exceeded", self.task_id)
        return remaining

    def consume(self, size: int) -> None:
        self.received += size
        if self.received > self.limits.received_bytes:
            raise CaptureError("cumulative_receive_limit", self.task_id)

    def receipt(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "collector": "mineru-v1-isolated",
            "endpoint_sha256": self.endpoint_sha256,
            "source": self.source.identity(),
            "outcome": self.outcome,
            "submission": self.submission,
            "task_id": self.task_id,
            "source_file_id": self.source_file_id,
            "source_verified": self.source_verified,
            "remote_status": self.remote_status,
            "cancellation": self.cancellation,
            "remote_drained": False,
            "received_bytes": self.received,
            "error": self.error,
            "artifact": dict(self.artifact),
            "cleanup_errors": list(self.cleanup_errors),
        }

    async def save(self) -> None:
        await _blocking(self.files.save_receipt, self.receipt())


class AsyncMinerUV1Collector:
    """原生V1单文件采集适配；借用应用级客户端和Gate，不隐式注册runner。"""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient,
        gate: Gate,
        limits: CaptureLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = validate_http_endpoint(base_url, name="MinerU V1 URL")
        self.client = client
        self.gate = gate
        self.limits = limits or CaptureLimits()
        self.clock = clock

    async def _request(
        self,
        run: _Run,
        method: str,
        route: str,
        *,
        status: int = 200,
        content: AsyncIterator[bytes] | None = None,
        params: dict[str, str] | None = None,
        artifact: bool = False,
        cleanup: bool = False,
    ) -> bytes:
        cap = self.limits.artifact_bytes if artifact else self.limits.control_bytes
        phase = (
            self.limits.cancel_seconds
            if cleanup
            else self.limits.upload_seconds
            if content is not None
            else self.limits.request_seconds
        )
        timeout = min(phase, run.remaining(cleanup=cleanup))
        headers = {"Accept-Encoding": "identity"}
        if content is not None:
            headers.update(
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(run.source.size),
                }
            )
        request = self.client.build_request(
            method,
            self.base_url + route,
            params=params,
            content=content,
            headers=headers,
            timeout=timeout,
        )
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.send(
                    request, stream=True, follow_redirects=False, auth=None
                )
                if response.status_code != status:
                    raise CaptureError(f"http_{response.status_code}", run.task_id)
                if (
                    response.headers.get("content-encoding", "identity").casefold()
                    != "identity"
                ):
                    raise CaptureError("unexpected_content_encoding", run.task_id)
                media = (
                    response.headers.get("content-type", "").split(";", 1)[0].casefold()
                )
                if media != (
                    "application/octet-stream" if artifact else "application/json"
                ):
                    raise CaptureError("unexpected_content_type", run.task_id)
                declared: int | None = None
                if "content-length" in response.headers:
                    value = response.headers["content-length"]
                    if not value.isascii() or not value.isdigit():
                        raise CaptureError("invalid_content_length", run.task_id)
                    declared = int(value)
                    if (
                        declared > cap
                        or declared > self.limits.received_bytes - run.received
                    ):
                        raise CaptureError("declared_receive_limit", run.task_id)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_raw(chunk_size=CHUNK_BYTES):
                    run.remaining(cleanup=cleanup)
                    run.consume(len(chunk))
                    size += len(chunk)
                    if size > cap:
                        raise CaptureError("response_receive_limit", run.task_id)
                    if artifact:
                        await _blocking(run.files.write_artifact, chunk)
                    else:
                        chunks.append(chunk)
                if declared is not None and size != declared:
                    raise CaptureError("content_length_mismatch", run.task_id)
                run.remaining(cleanup=cleanup)
                return b"".join(chunks)
        except (TimeoutError, httpx.TimeoutException):
            raise CaptureError("request_timeout", run.task_id) from None
        finally:
            if response is not None:
                await finish(asyncio.create_task(response.aclose()))

    async def _json(
        self,
        run: _Run,
        method: str,
        route: str,
        *,
        status: int = 200,
        content: AsyncIterator[bytes] | None = None,
        params: dict[str, str] | None = None,
        cleanup: bool = False,
    ) -> dict[str, object]:
        raw = await self._request(
            run,
            method,
            route,
            status=status,
            content=content,
            params=params,
            cleanup=cleanup,
        )
        try:
            value = decode_json(raw)
        except ValueError:
            raise CaptureError("invalid_response_json", run.task_id) from None
        run.remaining(cleanup=cleanup)
        return value

    async def _upload(self, run: _Run) -> AsyncIterator[bytes]:
        while True:
            run.remaining()
            chunk = await _blocking(run.files.read_source)
            if not chunk:
                break
            yield chunk

    def _frame(self, run: _Run, payload: dict[str, object]) -> TaskFrame:
        if run.task_id is None:
            raise RuntimeError("任务ID未保存")
        frame = task_frame(
            payload,
            run.source,
            self.base_url,
            expected_id=run.task_id,
            source_file_id=run.source_file_id,
        )
        if run.remote_status == "running" and frame.status == "queued":
            raise CaptureError("task_status_regression", run.task_id)
        run.source_file_id = frame.source_file_id
        run.remote_status = frame.status
        return frame

    async def _execute(self, run: _Run) -> None:
        check_health(await self._json(run, "GET", "/v1/health"))
        check_tiers(await self._json(run, "GET", "/v1/tiers"))
        run.outcome = "submitting"
        run.submission = "unknown"
        await run.save()
        payload = await self._json(
            run,
            "POST",
            "/v1/tasks",
            status=202,
            content=self._upload(run),
            params={
                "tier": "advanced",
                "ocr_mode": "auto",
                "page_range": "all",
                "output_formats": FORMAT,
                "filename": run.source.filename,
                # 4.0.12的SHA缓存短路会提前返回并关连接，真实大文件上传曾丢回包。
                # 完整上传仍由原生服务算SHA，随后用file metadata核验，不自动重提。
            },
        )
        run.task_id = identifier(payload.get("task_id"))
        run.submission = "task_observed"
        await run.save()
        frame = self._frame(run, payload)
        metadata = await self._json(run, "GET", f"/v1/files/{frame.source_file_id}")
        if (
            file_digest(
                metadata,
                file_id=frame.source_file_id,
                filename=run.source.filename,
                size=run.source.size,
                purpose="parse",
            )
            != run.source.sha256
        ):
            raise CaptureError("remote_source_digest_mismatch", run.task_id)
        run.source_verified = True
        run.submission = "source_verified"
        run.outcome = "polling"
        await run.save()
        while frame.status in RUNNING:
            await asyncio.sleep(min(self.limits.poll_seconds, run.remaining()))
            frame = self._frame(
                run, await self._json(run, "GET", f"/v1/tasks/{run.task_id}")
            )
        if frame.status != "completed":
            raise CaptureError(f"remote_{frame.status}", run.task_id)
        if frame.artifact_id is None or frame.artifact_bytes is None:
            raise CaptureError("missing_artifact", run.task_id)
        if frame.artifact_bytes > self.limits.artifact_bytes:
            raise CaptureError("declared_artifact_limit", run.task_id)
        metadata = await self._json(run, "GET", f"/v1/files/{frame.artifact_id}")
        expected_digest = file_digest(
            metadata,
            file_id=frame.artifact_id,
            filename=run.source.filename + ".structured_content.json",
            size=frame.artifact_bytes,
            purpose="parse_output",
        )
        run.outcome = "downloading"
        await run.save()
        await _blocking(run.files.start_artifact)
        before = run.received
        await self._request(
            run, "GET", f"/v1/files/{frame.artifact_id}/content", artifact=True
        )
        await _blocking(run.files.finish_artifact)
        if run.received - before != frame.artifact_bytes:
            raise CaptureError("artifact_size_mismatch", run.task_id)
        run.artifact = await _blocking(self._validate, run, expected_digest)
        # 本地资源关停也属于成功前置，不能先写completed再由finally推翻结果。
        await _blocking(run.files.close)
        run.outcome = "completed"
        await _blocking(run.files.publish, run.receipt(), run.remaining)
        run.remaining()

    def _validate(self, run: _Run, expected_digest: str) -> dict[str, object]:
        run.remaining()
        raw = read_limited(run.files.part, self.limits.artifact_bytes)
        if sha256(raw).hexdigest() != expected_digest:
            raise CaptureError("artifact_digest_mismatch", run.task_id)
        try:
            projection = project_structured(
                raw,
                source_sha256=run.source.sha256,
                expected_pages=run.source.pages,
                limits=replace(DEFAULT_LIMITS, wire_bytes=self.limits.artifact_bytes),
            )
            if not projection.document.markdown:
                raise ValueError("整份产物没有可索引正文")
        except ValueError:
            raise CaptureError("invalid_structured_artifact", run.task_id) from None
        run.remaining()
        return {
            "sha256": expected_digest,
            "bytes": len(raw),
            "canonical_sha256": projection.canonical_sha256,
            "pages": run.source.pages,
        }

    async def _cleanup_failure(self, run: _Run) -> None:
        if run.source_verified and run.remote_status not in TERMINAL:
            try:
                if run.received >= self.limits.received_bytes:
                    raise CaptureError("cumulative_receive_limit")
                payload = await self._json(
                    run, "DELETE", f"/v1/tasks/{run.task_id}", cleanup=True
                )
                if (
                    payload.get("task_id") != run.task_id
                    or not isinstance(payload.get("status"), str)
                    or payload["status"] not in TERMINAL
                ):
                    raise CaptureError("cancel_unconfirmed")
                run.remote_status = str(payload["status"])
                run.cancellation = (
                    "acknowledged"
                    if run.remote_status == "canceled"
                    else "remote_terminal"
                )
            except Exception as exc:
                run.cancellation = "unconfirmed"
                run.cleanup_errors.append(
                    exc.code if isinstance(exc, CaptureError) else type(exc).__name__
                )
        elif run.task_id is not None and not run.source_verified:
            run.cancellation = "skipped_unverified_source"
        try:
            await _blocking(run.files.discard_artifact)
        except Exception as exc:
            run.cleanup_errors.append(type(exc).__name__)
        try:
            await _blocking(run.files.close)
        except Exception as exc:
            run.cleanup_errors.append(type(exc).__name__)
        try:
            await run.save()
        except Exception as exc:
            run.cleanup_errors.append(type(exc).__name__)

    async def collect(self, source: CaptureSource, output: Path) -> dict[str, object]:
        """总预算从排队前开始；只对新目录操作，失败后不自动重新提交。"""
        run = _Run(
            source,
            CaptureFiles(output),
            self.limits,
            sha256(self.base_url.encode()).hexdigest(),
            self.clock() + self.limits.total_seconds,
            self.clock,
        )
        acquired = False
        error: BaseException | None = None
        try:
            async with asyncio.timeout(run.remaining()):
                if source.size > self.limits.source_bytes:
                    raise CaptureError("source_receive_limit")
                await _blocking(run.files.prepare, source, run.receipt())
                try:
                    async with asyncio.timeout(self.limits.gate_seconds):
                        await self.gate.acquire()
                except TimeoutError:
                    raise CaptureError("gate_wait_timeout") from None
                except Overloaded:
                    raise CaptureError("gate_overloaded") from None
                acquired = True
                await _blocking(run.files.copy_source, source, run.remaining)
                await self._execute(run)
                return run.receipt()
        except BaseException as exc:
            error = exc
            run.outcome = "failed"
            if isinstance(exc, asyncio.CancelledError):
                run.outcome = "cancelled"
            elif (
                isinstance(exc, TimeoutError)
                or isinstance(exc, CaptureError)
                and exc.code
                in {"deadline_exceeded", "request_timeout", "gate_wait_timeout"}
            ):
                run.outcome = "timed_out"
            run.error = (
                exc.code if isinstance(exc, CaptureError) else type(exc).__name__
            )
            run.artifact = {}
            await finish(asyncio.create_task(self._cleanup_failure(run)))
            if not isinstance(exc, Exception):
                raise
            code = run.error
            if run.submission == "unknown":
                code = "submission_unknown"
            elif isinstance(exc, TimeoutError):
                code = "deadline_exceeded"
            elif not isinstance(exc, CaptureError):
                code = (
                    "transport_error"
                    if isinstance(exc, httpx.HTTPError)
                    else "local_error"
                )
            raise CaptureError(code, run.task_id) from None
        finally:
            if acquired:
                await self.gate.__aexit__(
                    type(error) if error is not None else None,
                    error,
                    error.__traceback__ if error is not None else None,
                )
