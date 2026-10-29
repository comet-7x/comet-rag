from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from comet_rag._async import run_sync
from comet_rag.documents.schemas import (
    Block,
    ParsedDocument,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParsingInfo,
    RichText,
    TextRun,
)
from comet_rag.sources.types import LocalResource

from .base import _read_resource


class AsyncMinerUParser:
    """调用 MinerU 4.x V1 单请求接口；仅保留 Markdown，输出始终为 partial。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        expected_version: str,
        tier: str = "standard",
        ocr_mode: str = "auto",
        wait_timeout: int = 300,
        max_bytes: int = 128 * 1024 * 1024,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("MinerU requires an injected httpx.AsyncClient")
        url = httpx.URL(endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or not url.path.endswith("/v1/file_parse")
        ):
            raise ValueError(
                "MinerU endpoint must be an HTTP(S) /v1/file_parse URL without userinfo, query or fragment"
            )
        if (
            not isinstance(expected_version, str)
            or re.fullmatch(r"4\.[0-9]+\.[0-9]+", expected_version) is None
        ):
            raise ValueError(
                "MinerU requires the expected deployed 4.x release version"
            )
        if tier not in {"flash", "basic", "standard", "advanced"} or ocr_mode not in {
            "auto",
            "txt",
            "ocr",
        }:
            raise ValueError("unsupported MinerU tier or ocr_mode")
        if type(wait_timeout) is not int or not 1 <= wait_timeout <= 3600:
            raise ValueError("MinerU wait_timeout must be an integer from 1 to 3600")
        if any(
            type(value) is not int or value <= 0
            for value in (max_bytes, max_response_bytes)
        ):
            raise ValueError("MinerU byte budgets must be positive integers")
        self._client = client
        self._endpoint = url
        self._version = expected_version
        self._tier = tier
        self._ocr_mode = ocr_mode
        self._wait_timeout = wait_timeout
        self._max_bytes = max_bytes
        self._max_response_bytes = max_response_bytes

    async def parse(self, resource: LocalResource, *, output_dir: Path) -> ParseResult:
        data = await run_sync(self._pdf, resource)
        request = self._client.build_request(
            "POST",
            self._endpoint,
            params={
                "tier": self._tier,
                "ocr_mode": self._ocr_mode,
                "output_formats": "markdown",
                "response_format": "json",
                "wait_timeout": self._wait_timeout,
                "filename": "document.pdf",
                "sha256sum": resource.source.content_hash,
            },
            content=data,
            headers={
                "Content-Type": "application/octet-stream",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        response = await self._client.send(request, stream=True, follow_redirects=False)
        try:
            response.raise_for_status()
            if (
                response.status_code not in {200, 202}
                or response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "application/json"
            ):
                raise ValueError("MinerU requires a 200 JSON parsing result")
            if response.headers.get(
                "content-encoding", "identity"
            ).strip().lower() not in {"", "identity"}:
                raise ValueError("MinerU response must use identity encoding")
            header = response.headers.get("content-length")
            if header is not None and re.fullmatch(r"[0-9]+", header) is None:
                raise ValueError("invalid MinerU Content-Length")
            length = int(header) if header is not None else None
            if length is not None and length > self._max_response_bytes:
                raise ValueError("MinerU response exceeds byte budget")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > self._max_response_bytes:
                    raise ValueError("MinerU response exceeds byte budget")
                body.extend(chunk)
            if length is not None and len(body) != length:
                raise ValueError("MinerU response length does not match Content-Length")
        finally:
            active = sys.exception()
            try:
                await response.aclose()
            except Exception as cleanup:
                if active is None:
                    raise
                active.add_note(
                    f"MinerU response close failed: {type(cleanup).__name__}"
                )
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("task_id"), str)
            or not payload["task_id"]
        ):
            raise ValueError("MinerU response requires a task_id")
        if response.status_code == 202:
            # 上游等待超时/断连后任务仍运行；不自动重传或伪造完成结果。
            raise TimeoutError(
                f"MinerU wait budget exhausted; remote task {payload['task_id']} may still be running"
            )
        if (
            payload.get("status") != "completed"
            or payload.get("tier") != self._tier
            or payload.get("output_formats") != ["markdown"]
        ):
            raise ValueError("MinerU task status/tier/output mismatch")
        files = payload.get("files")
        if (
            not isinstance(files, list)
            or len(files) != 1
            or not isinstance(files[0], dict)
        ):
            raise ValueError("MinerU response must contain only the uploaded document")
        result = files[0]
        if (
            result.get("name") != "document.pdf"
            or result.get("status") != "completed"
            or result.get("error") is not None
        ):
            raise ValueError("MinerU uploaded document did not complete successfully")
        parsing = result.get("parse")
        if (
            not isinstance(parsing, dict)
            or parsing.get("parser_version") != self._version
        ):
            raise ValueError("MinerU response version mismatch")
        content = result.get("content")
        if (
            not isinstance(content, dict)
            or set(content) != {"markdown"}
            or not isinstance(content["markdown"], str)
        ):
            raise ValueError("MinerU document requires exactly one Markdown string")
        markdown = content["markdown"]
        return ParseResult(
            ParsedDocument(
                source=resource.source,
                parsing=ParsingInfo(
                    "mineru-http",
                    version=parsing["parser_version"],
                    config={
                        "tier": self._tier,
                        "ocr_mode": self._ocr_mode,
                        "output": "markdown-only",
                        "mapping_version": 2,
                    },
                ),
                blocks=(
                    Block(
                        "markdown_0", "text", rich_text=RichText((TextRun(markdown),))
                    ),
                ),
            ),
            ParseReport(
                status="partial",
                issues=(
                    ParseIssue(
                        "mineru_markdown_only",
                        "Only raw Markdown text is retained; native structure, tables, page coverage and image assets are unavailable.",
                    ),
                ),
            ),
        )

    def _pdf(self, resource: LocalResource) -> bytes:
        data = _read_resource(resource, self._max_bytes)
        if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
            raise ValueError(
                "MinerU input requires PDF signature and final %%EOF marker"
            )
        return data


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate MinerU response field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("nonfinite JSON value in MinerU response")
