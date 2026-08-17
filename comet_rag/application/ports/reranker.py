"""Reranker Port：公共入口直接返回可消费的排序结果。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Protocol, final

from comet_rag.models.content import (
    ContentInput,
    ImageContent,
    RankedDocument,
    RerankDocument,
    TextContent,
)


class _AsyncGate(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class RerankerPort(ABC):
    """应用层可使用的重排契约。

    ``rank/arank`` 是面向使用者的入口；``score/ascore`` 保留为兼容的低层
    接口，并由结构化入口复用。
    """

    _gate: _AsyncGate | None = None

    def bind_gate(self, gate: _AsyncGate | None) -> None:
        self._gate = gate

    @final
    async def ascore(self, query: Any, documents: Any, **kwargs: Any) -> list[float]:
        if self._gate is None:
            return await self._ascore(query, documents, **kwargs)
        async with self._gate:
            return await self._ascore(query, documents, **kwargs)

    @abstractmethod
    async def _ascore(
        self, query: Any, documents: Any, **kwargs: Any
    ) -> list[float]: ...

    @abstractmethod
    def score(self, query: Any, documents: Any, **kwargs: Any) -> list[float]:
        """兼容的同步低层打分接口。"""

    def _to_provider_input(self, content: ContentInput) -> Any:
        """把共享内容类型转换成供应商输入；文本模型使用默认实现。"""
        if isinstance(content, str):
            return content
        if any(isinstance(part, ImageContent) for part in content):
            raise TypeError(f"{type(self).__name__} 不支持图片重排输入")
        text_parts = [part.text for part in content if isinstance(part, TextContent)]
        if len(text_parts) == len(content):
            return "\n".join(text_parts)
        raise TypeError("不支持的重排内容类型")

    @staticmethod
    def _normalize_documents(
        documents: Sequence[str | RerankDocument],
    ) -> list[RerankDocument]:
        return [
            document
            if isinstance(document, RerankDocument)
            else RerankDocument(content=document)
            for document in documents
        ]

    @staticmethod
    def _ranked(
        documents: Sequence[RerankDocument],
        scores: Sequence[float],
        top_k: int | None,
    ) -> list[RankedDocument]:
        if len(scores) != len(documents):
            raise ValueError(
                f"重排返回 {len(scores)} 个分数，但请求包含 {len(documents)} 个候选"
            )
        if top_k is not None and top_k <= 0:
            raise ValueError(f"top_k 必须大于 0，收到 {top_k}")
        ranked = [
            RankedDocument(index=index, score=score, document=document)
            for index, (document, score) in enumerate(
                zip(documents, scores, strict=True)
            )
        ]
        ranked.sort(key=lambda item: (-item.score, item.index))
        return ranked if top_k is None else ranked[:top_k]

    @final
    def rank(
        self,
        query: ContentInput,
        documents: Sequence[str | RerankDocument],
        /,
        *,
        top_k: int | None = None,
        **kwargs: Any,
    ) -> list[RankedDocument]:
        """同步重排并返回携带原始候选的有序结果。"""
        normalized = self._normalize_documents(documents)
        scores = self.score(
            self._to_provider_input(query),
            [self._to_provider_input(document.content) for document in normalized],
            **kwargs,
        )
        return self._ranked(normalized, scores, top_k)

    @final
    async def arank(
        self,
        query: ContentInput,
        documents: Sequence[str | RerankDocument],
        /,
        *,
        top_k: int | None = None,
        **kwargs: Any,
    ) -> list[RankedDocument]:
        """异步重排并返回携带原始候选的有序结果。"""
        normalized = self._normalize_documents(documents)
        scores = await self.ascore(
            self._to_provider_input(query),
            [self._to_provider_input(document.content) for document in normalized],
            **kwargs,
        )
        return self._ranked(normalized, scores, top_k)

    async def aclose(self) -> None:
        return None


BaseReranker = RerankerPort

__all__ = ["BaseReranker", "RerankerPort"]
