from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from comet_rag.ports import (
    DocumentProtocolError,
    DocumentUpstreamError,
    ExtractedDocument,
)

MINERU_API_PROTOCOL_VERSION = 2
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


@dataclass(frozen=True, slots=True)
class _ServerInfo:
    version: str


@dataclass(frozen=True, slots=True)
class _SubmittedTask:
    task_id: str


class MinerUDocumentExtractor:
    """外部 `mineru-api` / `mineru-router` 的 HTTP 文档提取适配器。"""

    def __init__(
        self,
        base_url: str,
        *,
        backend: str = "pipeline",
        parse_method: str = "auto",
        language: str = "ch",
        formula: bool = True,
        table: bool = True,
        poll_interval_seconds: float = 1.0,
        sync_client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
        sleep: SyncSleeper = time.sleep,
        asleep: AsyncSleeper = asyncio.sleep,
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

        self._base_url = base_url.rstrip("/")
        self._backend = backend
        self._parse_method = parse_method
        self._language = language
        self._formula = formula
        self._table = table
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._asleep = asleep

        self._owns_sync_client = sync_client is None
        self._owns_async_client = async_client is None
        self.sync_client = sync_client or httpx.Client()
        self.async_client = async_client or httpx.AsyncClient()

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    @staticmethod
    def _payload(response: httpx.Response, operation: str) -> Mapping[str, Any]:
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise DocumentProtocolError(
                f"MinerU {operation} 响应不是合法 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DocumentProtocolError(f"MinerU {operation} 响应必须是 JSON object")
        return payload

    @classmethod
    def _server_info(cls, response: httpx.Response) -> _ServerInfo:
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
    def _submitted_task(cls, response: httpx.Response) -> _SubmittedTask:
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
    def _task_status(cls, response: httpx.Response, task_id: str) -> str:
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

    @staticmethod
    def _document(
        response: httpx.Response, server_info: _ServerInfo, backend: str
    ) -> ExtractedDocument:
        payload = MinerUDocumentExtractor._payload(response, "task result")
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
        return ExtractedDocument(
            markdown=markdown,
            metadata={
                "provider": "mineru",
                "backend": backend,
                "version": server_info.version,
                "protocol_version": MINERU_API_PROTOCOL_VERSION,
            },
        )

    def _submit(self, path: Path, *, filename: str, media_type: str) -> _SubmittedTask:
        with path.open("rb") as stream:
            response = self.sync_client.post(
                self._url("/tasks"),
                data=self._form_data(),
                files={"files": (filename, stream, media_type)},
            )
        return self._submitted_task(response)

    async def _asubmit(
        self, path: Path, *, filename: str, media_type: str
    ) -> _SubmittedTask:
        with path.open("rb") as stream:
            response = await self.async_client.post(
                self._url("/tasks"),
                data=self._form_data(),
                files={"files": (filename, stream, media_type)},
            )
        return self._submitted_task(response)

    def _wait(self, task: _SubmittedTask) -> None:
        task_id = quote(task.task_id, safe="")
        while True:
            response = self.sync_client.get(self._url(f"/tasks/{task_id}"))
            if self._task_status(response, task.task_id) == "completed":
                return
            self._sleep(self._poll_interval_seconds)

    async def _await(self, task: _SubmittedTask) -> None:
        task_id = quote(task.task_id, safe="")
        while True:
            response = await self.async_client.get(self._url(f"/tasks/{task_id}"))
            if self._task_status(response, task.task_id) == "completed":
                return
            await self._asleep(self._poll_interval_seconds)

    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        server_info = self._server_info(self.sync_client.get(self._url("/health")))
        task = self._submit(path, filename=filename, media_type=media_type)
        self._wait(task)
        task_id = quote(task.task_id, safe="")
        response = self.sync_client.get(self._url(f"/tasks/{task_id}/result"))
        return self._document(response, server_info, self._backend)

    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        health = await self.async_client.get(self._url("/health"))
        server_info = self._server_info(health)
        task = await self._asubmit(path, filename=filename, media_type=media_type)
        await self._await(task)
        task_id = quote(task.task_id, safe="")
        response = await self.async_client.get(self._url(f"/tasks/{task_id}/result"))
        return self._document(response, server_info, self._backend)

    async def aclose(self) -> None:
        """只关闭当前适配器创建的客户端。"""
        if self._owns_async_client:
            await self.async_client.aclose()
        if self._owns_sync_client:
            self.sync_client.close()


__all__ = ["MINERU_API_PROTOCOL_VERSION", "MinerUDocumentExtractor"]
