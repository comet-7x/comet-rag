"""Reranker 适配器基类：翻译成供应商格式，再打分，再排序。

契约在 :mod:`comet_rag.ports.reranker`；本模块是**可选的**
实现复用 —— 满足 ``RerankerPort`` 形状的对象不必继承它。

## ``ProviderInput`` 是这里的，不是 Port 的

``rank``/``arank`` 全程用本项目自己的类型；``score``/``ascore`` 收的是
**已翻译成供应商格式**的东西，文本模型是 ``str``，Qwen 多模态是
``str | ScoreMultiModalParam``。这个类型因适配器而异，所以做成类型参数：

    class Qwen3VLReranker(BaseReranker[str | ScoreMultiModalParam]): ...
    class MyTextReranker(BaseReranker[str]): ...

写成 ``Any`` 的话，``_to_provider_input`` 产出什么、``score`` 收什么就完全
失去关联，传错了要到发请求时才知道。

**参数化不是可选的**：``class X(BaseReranker)`` 会被静默当作
``BaseReranker[Unknown]``，于是类型参数白加了。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, cast, final

from comet_rag.infrastructure.providers.base import GatedModel
from comet_rag.ports.content import (
    ContentInput,
    ImageContent,
    RankedDocument,
    RerankDocument,
    TextContent,
)
from comet_rag.ports.reranker import RerankerPort


class BaseReranker[ProviderInput](GatedModel, ABC):
    """Reranker 适配器的共享实现。"""

    # ── 适配器必须实现的两个扩展点 ─────────────────────────────────────────

    @abstractmethod
    async def _ascore(
        self,
        query: ProviderInput,
        documents: Sequence[ProviderInput],
        **kwargs: Any,
    ) -> list[float]:
        """适配器真正执行异步打分请求的地方。"""

    @abstractmethod
    def score(
        self,
        query: ProviderInput,
        documents: Sequence[ProviderInput],
        **kwargs: Any,
    ) -> list[float]:
        """同步低层打分接口。入参已是供应商格式（由 ``_to_provider_input``
        翻译），所以类型随适配器而定。"""

    @final
    async def ascore(
        self,
        query: ProviderInput,
        documents: Sequence[ProviderInput],
        **kwargs: Any,
    ) -> list[float]:
        """异步低层打分接口。受闸门保护，不可覆写。"""
        return await self._through_gate(
            lambda: self._ascore(query, documents, **kwargs)
        )

    # ── 供应商格式翻译 ─────────────────────────────────────────────────────

    def _to_provider_input(self, content: ContentInput) -> ProviderInput:
        """把共享内容类型转换成供应商输入；纯文本模型使用默认实现。

        默认实现产出 ``str``，因此只对 ``ProviderInput`` 含 ``str`` 的适配器
        成立（``BaseReranker[str]`` 与 Qwen 的
        ``BaseReranker[str | ScoreMultiModalParam]`` 都满足）。把
        ``ProviderInput`` 参数化成不含 ``str`` 的类型时**必须覆写本方法** ——
        那个 cast 就是这条约定的记号。
        """
        if isinstance(content, str):
            return cast("ProviderInput", content)
        if any(isinstance(part, ImageContent) for part in content):
            raise TypeError(f"{type(self).__name__} 不支持图片重排输入")
        text_parts = [part.text for part in content if isinstance(part, TextContent)]
        if len(text_parts) == len(content):
            return cast("ProviderInput", "\n".join(text_parts))
        raise TypeError("不支持的重排内容类型")

    # ── 排序 ───────────────────────────────────────────────────────────────

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

    # ── 面向使用者的公共入口 ───────────────────────────────────────────────

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


__all__ = ["BaseReranker", "RerankerPort"]
