from __future__ import annotations

import json
import re
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
    """固定 MinerU 2.6.8 /file_parse；parse_method 仅记录请求值，服务不返回实际选择。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        expected_version: str,
        backend: str = "pipeline",
        parse_method: str = "auto",
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
            or not url.path.endswith("/file_parse")
        ):
            raise ValueError(
                "MinerU endpoint must be an HTTP(S) /file_parse URL without userinfo, query or fragment"
            )
        if expected_version != "2.6.8":
            raise ValueError(
                "MinerU adapter supports the official 2.6.8 HTTP schema only"
            )
        if backend not in {
            "pipeline",
            "vlm-transformers",
            "vlm-mlx-engine",
            "vlm-vllm-async-engine",
            "vlm-lmdeploy-engine",
            "vlm-http-client",
        } or parse_method not in {"auto", "txt", "ocr"}:
            raise ValueError("unsupported MinerU backend or parse_method")
        if any(
            type(value) is not int or value <= 0
            for value in (max_bytes, max_response_bytes)
        ):
            raise ValueError("MinerU byte budgets must be positive integers")
        self._client = client
        self._endpoint = url
        self._version = expected_version
        self._backend = backend
        self._parse_method = parse_method
        self._max_bytes = max_bytes
        self._max_response_bytes = max_response_bytes

    async def parse(self, resource: LocalResource, *, output_dir: Path) -> ParseResult:
        data = await run_sync(self._pdf, resource)
        options = {
            "backend": self._backend,
            "parse_method": self._parse_method,
            "return_md": "true",
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_content_list": "false",
            "return_images": "false",
            "response_format_zip": "false",
        }
        request = self._client.build_request(
            "POST",
            self._endpoint,
            data=options,
            files={"files": ("document.pdf", data, "application/pdf")},
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
        response = await self._client.send(request, stream=True, follow_redirects=False)
        try:
            response.raise_for_status()
            if (
                response.status_code != 200
                or response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "application/json"
            ):
                raise ValueError("MinerU requires a 200 JSON response")
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
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(body) + len(chunk) > self._max_response_bytes:
                    raise ValueError("MinerU response exceeds byte budget")
                body.extend(chunk)
            if length is not None and len(body) != length:
                raise ValueError("MinerU response length does not match Content-Length")
        finally:
            await response.aclose()
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        if not isinstance(payload, dict) or set(payload) != {
            "backend",
            "version",
            "results",
        }:
            raise ValueError("unknown MinerU response schema")
        if payload["version"] != self._version or payload["backend"] != self._backend:
            raise ValueError("MinerU response version/backend mismatch")
        results = payload["results"]
        if not isinstance(results, dict) or set(results) != {"document"}:
            raise ValueError("MinerU response must contain only the uploaded document")
        result = results["document"]
        if (
            not isinstance(result, dict)
            or set(result) != {"md_content"}
            or not isinstance(result["md_content"], str)
        ):
            raise ValueError("MinerU document requires exactly one Markdown string")
        markdown = result["md_content"]
        return ParseResult(
            ParsedDocument(
                source=resource.source,
                parsing=ParsingInfo(
                    "mineru-http",
                    version=payload["version"],
                    config={
                        "backend": payload["backend"],
                        "parse_method": self._parse_method,
                        "output": "markdown-only",
                        "mapping_version": 1,
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
