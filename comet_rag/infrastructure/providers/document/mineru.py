from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, final
from urllib.parse import quote, urlsplit

import httpx

from comet_rag.ports import (
    DocumentProtocolError,
    DocumentResourceLimitExceeded,
    DocumentUpstreamError,
    ExtractedDocument,
    RetryableDocumentUpstreamError,
)
from comet_rag.ports.gate import GatedResource

MINERU_API_PROTOCOL_VERSION = 2
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 300.0
DEFAULT_PARSE_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_MARKDOWN_BYTES = 8 * 1024 * 1024

_RUNNING_STATUSES = frozenset({"pending", "processing"})
_OUTPUT_FLAGS: dict[str, str] = {
    "return_md": "true",
    "return_middle_json": "false",
    "return_model_output": "false",
    "return_content_list": "false",
    "return_images": "false",
    "response_format_zip": "false",
    "return_original_file": "false",
    "client_side_output_generation": "false",
}

SyncSleeper = Callable[[float], None]
AsyncSleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _ServerInfo:
    version: str


@dataclass(frozen=True, slots=True)
class _SubmittedTask:
    task_id: str


@dataclass(frozen=True, slots=True)
class _BufferedResponse:
    status_code: int
    content: bytes


class _RemoteTaskMissing(RuntimeError):
    """MinerU 的进程内任务状态已丢失，可以在本次预算内重提一次。"""


class MinerUDocumentExtractor(GatedResource):
    """外部 `mineru-api` / `mineru-router` 的有界 HTTP 适配器。"""

    def __init__(
        self,
        base_url: str,
        *,
        backend: str = "pipeline",
        parse_method: str = "auto",
        language: str = "ch",
        formula: bool = True,
        table: bool = True,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        upload_timeout_seconds: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 1.0,
        parse_timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_markdown_bytes: int = DEFAULT_MAX_MARKDOWN_BYTES,
        sync_client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
        sleep: SyncSleeper = time.sleep,
        asleep: AsyncSleeper = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MinerU base_url 必须是绝对 HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("MinerU base_url 不能包含 query 或 fragment")
        if not backend.strip():
            raise ValueError("MinerU backend 不能为空")
        if parse_method not in {"auto", "txt", "ocr"}:
            raise ValueError("MinerU parse_method 必须是 auto、txt 或 ocr")
        if not language.strip():
            raise ValueError("MinerU language 不能为空")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds 不能小于 0")
        self._require_positive("connect_timeout_seconds", connect_timeout_seconds)
        self._require_positive("request_timeout_seconds", request_timeout_seconds)
        self._require_positive("upload_timeout_seconds", upload_timeout_seconds)
        self._require_positive("parse_timeout_seconds", parse_timeout_seconds)
        self._require_positive("max_response_bytes", max_response_bytes)
        self._require_positive("max_markdown_bytes", max_markdown_bytes)

        self._base_url = base_url.rstrip("/")
        self._backend = backend
        self._parse_method = parse_method
        self._language = language
        self._formula = formula
        self._table = table
        self._connect_timeout_seconds = connect_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._upload_timeout_seconds = upload_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._parse_timeout_seconds = parse_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_markdown_bytes = max_markdown_bytes
        self._sleep = sleep
        self._asleep = asleep
        self._clock = clock

        self._owns_sync_client = sync_client is None
        self._owns_async_client = async_client is None
        self.sync_client = sync_client or httpx.Client()
        self.async_client = async_client or httpx.AsyncClient()

    @staticmethod
    def _require_positive(name: str, value: float) -> None:
        if value <= 0:
            raise ValueError(f"{name} 必须大于 0")

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise RetryableDocumentUpstreamError(
                f"MinerU 总解析超时（{self._parse_timeout_seconds:g}s）"
            )
        return remaining

    def _timeout(self, deadline: float, *, upload: bool = False) -> httpx.Timeout:
        remaining = self._remaining(deadline)
        request = min(self._request_timeout_seconds, remaining)
        connect = min(self._connect_timeout_seconds, remaining)
        write_budget = (
            self._upload_timeout_seconds if upload else self._request_timeout_seconds
        )
        return httpx.Timeout(
            timeout=request,
            connect=connect,
            read=request,
            write=min(write_budget, remaining),
            pool=request,
        )

    @staticmethod
    def _raise_for_status(
        status_code: int, operation: str, *, task_may_be_missing: bool
    ) -> None:
        if 200 <= status_code < 300:
            return
        if status_code == 404 and task_may_be_missing:
            raise _RemoteTaskMissing(f"MinerU {operation} 返回 404")
        if status_code == 429 or status_code >= 500:
            raise RetryableDocumentUpstreamError(
                f"MinerU {operation} 返回可重试 HTTP {status_code}"
            )
        raise DocumentUpstreamError(f"MinerU {operation} 返回确定性 HTTP {status_code}")

    def _read_response(self, response: httpx.Response, deadline: float) -> bytes:
        self._check_declared_size(response)
        content = bytearray()
        for chunk in response.iter_bytes():
            self._remaining(deadline)
            self._check_accumulated_size(len(content) + len(chunk))
            content.extend(chunk)
        return bytes(content)

    async def _aread_response(self, response: httpx.Response, deadline: float) -> bytes:
        self._check_declared_size(response)
        content = bytearray()
        async for chunk in response.aiter_bytes():
            self._remaining(deadline)
            self._check_accumulated_size(len(content) + len(chunk))
            content.extend(chunk)
        return bytes(content)

    def _check_declared_size(self, response: httpx.Response) -> None:
        content_length = response.headers.get("content-length")
        if content_length is None:
            return
        try:
            declared_size = int(content_length)
        except ValueError:
            return
        if declared_size > self._max_response_bytes:
            raise DocumentResourceLimitExceeded(
                "MinerU HTTP 响应体超过限制："
                f"{declared_size} > {self._max_response_bytes} bytes"
            )

    def _check_accumulated_size(self, size: int) -> None:
        if size > self._max_response_bytes:
            raise DocumentResourceLimitExceeded(
                f"MinerU HTTP 响应体超过限制：> {self._max_response_bytes} bytes"
            )

    @staticmethod
    def _transport_error(
        exc: httpx.RequestError, operation: str, *, upload: bool
    ) -> RetryableDocumentUpstreamError:
        if isinstance(exc, httpx.ConnectTimeout):
            detail = "连接超时"
        elif upload and isinstance(exc, httpx.WriteTimeout):
            detail = "上传超时"
        elif isinstance(exc, httpx.TimeoutException):
            detail = "单次请求超时"
        else:
            detail = "网络错误"
        return RetryableDocumentUpstreamError(f"MinerU {operation} {detail}：{exc!s}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        deadline: float,
        upload: bool = False,
        task_may_be_missing: bool = False,
        **kwargs: Any,
    ) -> _BufferedResponse:
        try:
            with self.sync_client.stream(
                method,
                self._url(path),
                timeout=self._timeout(deadline, upload=upload),
                **kwargs,
            ) as response:
                self._raise_for_status(
                    response.status_code,
                    operation,
                    task_may_be_missing=task_may_be_missing,
                )
                return _BufferedResponse(
                    response.status_code, self._read_response(response, deadline)
                )
        except httpx.RequestError as exc:
            raise self._transport_error(exc, operation, upload=upload) from exc

    async def _arequest(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        deadline: float,
        upload: bool = False,
        task_may_be_missing: bool = False,
        **kwargs: Any,
    ) -> _BufferedResponse:
        try:
            async with self.async_client.stream(
                method,
                self._url(path),
                timeout=self._timeout(deadline, upload=upload),
                **kwargs,
            ) as response:
                self._raise_for_status(
                    response.status_code,
                    operation,
                    task_may_be_missing=task_may_be_missing,
                )
                return _BufferedResponse(
                    response.status_code,
                    await self._aread_response(response, deadline),
                )
        except httpx.RequestError as exc:
            raise self._transport_error(exc, operation, upload=upload) from exc

    @staticmethod
    def _payload(response: _BufferedResponse, operation: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(response.content)
        except (UnicodeError, ValueError) as exc:
            raise DocumentProtocolError(
                f"MinerU {operation} 响应不是合法 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DocumentProtocolError(f"MinerU {operation} 响应必须是 JSON object")
        return payload

    @classmethod
    def _server_info(cls, response: _BufferedResponse) -> _ServerInfo:
        payload = cls._payload(response, "health")
        if payload.get("status") != "healthy":
            raise DocumentUpstreamError("MinerU 服务未处于 healthy 状态")
        protocol_version = payload.get("protocol_version")
        if (
            type(protocol_version) is not int
            or protocol_version != MINERU_API_PROTOCOL_VERSION
        ):
            raise DocumentProtocolError(
                "MinerU protocol_version 不兼容："
                f"期望 {MINERU_API_PROTOCOL_VERSION}，收到 {protocol_version!r}"
            )
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            raise DocumentProtocolError("MinerU health 响应缺少非空 version")
        return _ServerInfo(version=version)

    @classmethod
    def _submitted_task(cls, response: _BufferedResponse) -> _SubmittedTask:
        payload = cls._payload(response, "task submission")
        if response.status_code != 202:
            raise DocumentProtocolError(
                f"MinerU task submission 应返回 202，收到 {response.status_code}"
            )
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise DocumentProtocolError("MinerU task submission 缺少非空 task_id")
        return _SubmittedTask(task_id=task_id)

    @classmethod
    def _task_status(cls, response: _BufferedResponse, task_id: str) -> str:
        payload = cls._payload(response, "task status")
        returned_id = payload.get("task_id")
        if returned_id is not None and returned_id != task_id:
            raise DocumentProtocolError(
                f"MinerU task status 返回了不匹配的 task_id：{returned_id!r}"
            )
        status = payload.get("status")
        if not isinstance(status, str):
            raise DocumentProtocolError("MinerU task status 缺少字符串 status")
        if status == "failed":
            detail = payload.get("error") or "未提供错误信息"
            raise DocumentUpstreamError(f"MinerU task {task_id} 失败：{detail}")
        if status not in {*_RUNNING_STATUSES, "completed"}:
            raise DocumentProtocolError(f"MinerU 返回未知任务状态：{status!r}")
        return status

    def _form_data(self) -> dict[str, str]:
        return {
            "lang_list": self._language,
            "backend": self._backend,
            "parse_method": self._parse_method,
            "formula_enable": str(self._formula).lower(),
            "table_enable": str(self._table).lower(),
            **_OUTPUT_FLAGS,
        }

    def _document(
        self, response: _BufferedResponse, server_info: _ServerInfo
    ) -> ExtractedDocument:
        payload = self._payload(response, "task result")
        results = payload.get("results")
        if not isinstance(results, dict) or len(results) != 1:
            count = len(results) if isinstance(results, dict) else "非 object"
            raise DocumentProtocolError(
                f"MinerU task result 必须恰好包含一个 results 项，收到 {count}"
            )
        result = next(iter(results.values()))
        if not isinstance(result, dict):
            raise DocumentProtocolError("MinerU results 项必须是 JSON object")
        markdown = result.get("md_content")
        if not isinstance(markdown, str):
            raise DocumentProtocolError("MinerU result 缺少字符串 md_content")
        markdown_size = len(markdown.encode("utf-8"))
        if markdown_size > self._max_markdown_bytes:
            raise DocumentResourceLimitExceeded(
                "MinerU Markdown 超过限制："
                f"{markdown_size} > {self._max_markdown_bytes} bytes"
            )
        return ExtractedDocument(
            markdown=markdown,
            metadata={
                "provider": "mineru",
                "backend": self._backend,
                "version": server_info.version,
                "protocol_version": MINERU_API_PROTOCOL_VERSION,
            },
        )

    def _submit(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        deadline: float,
    ) -> _SubmittedTask:
        with path.open("rb") as stream:
            response = self._request(
                "POST",
                "/tasks",
                operation="task submission",
                deadline=deadline,
                upload=True,
                data=self._form_data(),
                files={"files": (filename, stream, media_type)},
            )
        return self._submitted_task(response)

    async def _asubmit(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        deadline: float,
    ) -> _SubmittedTask:
        with path.open("rb") as stream:
            response = await self._arequest(
                "POST",
                "/tasks",
                operation="task submission",
                deadline=deadline,
                upload=True,
                data=self._form_data(),
                files={"files": (filename, stream, media_type)},
            )
        return self._submitted_task(response)

    def _wait(self, task: _SubmittedTask, deadline: float) -> None:
        task_id = quote(task.task_id, safe="")
        while True:
            response = self._request(
                "GET",
                f"/tasks/{task_id}",
                operation="task status",
                deadline=deadline,
                task_may_be_missing=True,
            )
            if self._task_status(response, task.task_id) == "completed":
                return
            self._sleep(min(self._poll_interval_seconds, self._remaining(deadline)))

    async def _await(self, task: _SubmittedTask, deadline: float) -> None:
        task_id = quote(task.task_id, safe="")
        while True:
            response = await self._arequest(
                "GET",
                f"/tasks/{task_id}",
                operation="task status",
                deadline=deadline,
                task_may_be_missing=True,
            )
            if self._task_status(response, task.task_id) == "completed":
                return
            await self._asleep(
                min(self._poll_interval_seconds, self._remaining(deadline))
            )

    def _result(
        self, task: _SubmittedTask, server_info: _ServerInfo, deadline: float
    ) -> ExtractedDocument:
        task_id = quote(task.task_id, safe="")
        response = self._request(
            "GET",
            f"/tasks/{task_id}/result",
            operation="task result",
            deadline=deadline,
            task_may_be_missing=True,
        )
        return self._document(response, server_info)

    async def _aresult(
        self, task: _SubmittedTask, server_info: _ServerInfo, deadline: float
    ) -> ExtractedDocument:
        task_id = quote(task.task_id, safe="")
        response = await self._arequest(
            "GET",
            f"/tasks/{task_id}/result",
            operation="task result",
            deadline=deadline,
            task_may_be_missing=True,
        )
        return self._document(response, server_info)

    def _extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        deadline = self._clock() + self._parse_timeout_seconds
        health = self._request("GET", "/health", operation="health", deadline=deadline)
        server_info = self._server_info(health)
        task = self._submit(
            path, filename=filename, media_type=media_type, deadline=deadline
        )
        for attempt in range(2):
            try:
                self._wait(task, deadline)
                return self._result(task, server_info, deadline)
            except _RemoteTaskMissing as exc:
                if attempt == 1:
                    raise RetryableDocumentUpstreamError(
                        "MinerU 远端任务连续丢失，已停止重提"
                    ) from exc
                self._remaining(deadline)
                task = self._submit(
                    path,
                    filename=filename,
                    media_type=media_type,
                    deadline=deadline,
                )
        raise AssertionError("unreachable")

    async def _aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        deadline = self._clock() + self._parse_timeout_seconds
        health = await self._arequest(
            "GET", "/health", operation="health", deadline=deadline
        )
        server_info = self._server_info(health)
        task = await self._asubmit(
            path, filename=filename, media_type=media_type, deadline=deadline
        )
        for attempt in range(2):
            try:
                await self._await(task, deadline)
                return await self._aresult(task, server_info, deadline)
            except _RemoteTaskMissing as exc:
                if attempt == 1:
                    raise RetryableDocumentUpstreamError(
                        "MinerU 远端任务连续丢失，已停止重提"
                    ) from exc
                self._remaining(deadline)
                task = await self._asubmit(
                    path,
                    filename=filename,
                    media_type=media_type,
                    deadline=deadline,
                )
        raise AssertionError("unreachable")

    @final
    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        """同步提取入口；服务装配后与异步入口共用 MinerU 闸门。"""
        return self._through_gate_sync(
            lambda: self._extract(path, filename=filename, media_type=media_type)
        )

    @final
    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        """异步提取入口；等待许可期间取消不会泄漏闸门名额。"""
        return await self._through_gate(
            lambda: self._aextract(path, filename=filename, media_type=media_type)
        )

    async def aclose(self) -> None:
        """只关闭当前适配器创建的客户端。"""
        if self._owns_async_client:
            await self.async_client.aclose()
        if self._owns_sync_client:
            self.sync_client.close()


__all__ = ["MINERU_API_PROTOCOL_VERSION", "MinerUDocumentExtractor"]
