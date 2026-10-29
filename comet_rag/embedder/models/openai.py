from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from typing import Literal

import httpx

from comet_rag._async import close_async, map_concurrently
from comet_rag.content import ContentItem, ImagePart, content_parts, text_view

from ..types import EmbeddingResult, EmbeddingSpace, vector_values


class AsyncOpenAITextEmbedder:
    """只实现文本 embeddings 协议；注入客户端的超时、认证和关闭由创建者负责。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        space: EmbeddingSpace,
        endpoint: str = "embeddings",
        batch_size: int = 32,
        concurrency: int = 2,
        max_chars: int = 32_000,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        for value in (batch_size, concurrency, max_chars, max_response_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("model budgets must be positive integers")
        self.client = client
        self.space = space
        self.endpoint = endpoint
        self.batch_size = batch_size
        self.concurrency = concurrency
        self.max_chars = max_chars
        self.max_response_bytes = max_response_bytes

    async def embed(
        self, items: Sequence[ContentItem], *, role: Literal["query", "document"]
    ) -> tuple[EmbeddingResult, ...]:
        if role not in ("query", "document"):
            raise ValueError("unsupported embedding role")
        if any(not isinstance(item, ContentItem) for item in items) or len(
            {item.id for item in items}
        ) != len(items):
            raise ValueError("batch IDs must be unique")
        prefix = (
            self.space.query_prefix if role == "query" else self.space.document_prefix
        )
        texts = []
        for item in items:
            if any(isinstance(part, ImagePart) for part in content_parts(item.content)):
                raise ValueError(
                    "text embeddings protocol does not support image input"
                )
            text = prefix + text_view(item.content)
            if len(text) > self.max_chars:
                raise ValueError("embedding input exceeds configured character budget")
            texts.append(text)
        batches = [
            (
                tuple(items[start : start + self.batch_size]),
                texts[start : start + self.batch_size],
            )
            for start in range(0, len(items), self.batch_size)
        ]

        async def request(
            batch: tuple[tuple[ContentItem, ...], list[str]],
        ) -> tuple[EmbeddingResult, ...]:
            selected, inputs = batch
            request = self.client.build_request(
                "POST",
                self.endpoint,
                json={
                    "model": self.space.model,
                    "input": inputs,
                    "encoding_format": "float",
                },
                headers={"Accept-Encoding": "identity"},
            )
            response = await self.client.send(
                request, stream=True, follow_redirects=False
            )
            try:
                response.raise_for_status()
                if response.headers.get(
                    "content-encoding", "identity"
                ).strip().lower() not in {"", "identity"}:
                    raise ValueError("embedding response must use identity encoding")
                header = response.headers.get("content-length")
                if header is not None and re.fullmatch(r"[0-9]+", header) is None:
                    raise ValueError("invalid embedding Content-Length")
                length = int(header) if header is not None else None
                if length is not None and length > self.max_response_bytes:
                    raise ValueError("embedding response exceeds byte budget")
                if not isinstance(response.stream, httpx.AsyncByteStream):
                    raise TypeError(
                        "embedding requires an asynchronous response stream"
                    )
                body = bytearray()
                if response.is_stream_consumed:
                    # 注入客户端的 hook 可提前缓存正文；不再读取已关闭的流。
                    if len(response.content) > self.max_response_bytes:
                        raise ValueError("embedding response exceeds byte budget")
                    body.extend(response.content)
                else:
                    # identity 原始流避免隐式解压和自动关闭绕过预算与排干。
                    async for data in response.stream:
                        if len(body) + len(data) > self.max_response_bytes:
                            raise ValueError("embedding response exceeds byte budget")
                        body.extend(data)
                if length is not None and len(body) != length:
                    raise ValueError(
                        "embedding response length does not match Content-Length"
                    )
            finally:
                await close_async(
                    response.aclose(), sys.exception(), "embedding response"
                )
            import json

            payload = json.loads(body)
            values = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(values, list) or len(values) != len(selected):
                raise ValueError("embedding response count mismatch")
            results: dict[int, EmbeddingResult] = {}
            for entry in values:
                if not isinstance(entry, dict):
                    raise ValueError("invalid embedding entry")
                index = entry.get("index")
                vector = entry.get("embedding")
                if (
                    type(index) is not int
                    or not 0 <= index < len(selected)
                    or index in results
                    or not isinstance(vector, list)
                ):
                    raise ValueError("invalid or duplicate embedding index")
                results[index] = EmbeddingResult(
                    selected[index].id,
                    vector_values(vector, self.space.dimension),
                    self.space.space_id,
                )
            return tuple(results[index] for index in range(len(selected)))

        groups = await map_concurrently(request, batches, limit=self.concurrency)
        return tuple(result for group in groups for result in group)
