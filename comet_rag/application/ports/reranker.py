"""Reranker Port：公共入口直接返回可消费的排序结果。

## ``score`` 说的是供应商的话，所以它是个类型参数

``rank``/``arank`` 面向使用者，收 ``ContentInput``、还 ``RankedDocument``，
全程是本项目自己的类型。而 ``score``/``ascore`` 是低层入口，收的是**已经
翻译成供应商格式**的东西 —— 文本模型是 ``str``，Qwen 多模态是
``str | ScoreMultiModalParam``。

这个类型因适配器而异，所以用类型参数表达，而不是退回 ``Any``：

    class Qwen3VLReranker(RerankerPort[str | ScoreMultiModalParam]): ...
    class MyTextReranker(RerankerPort[str]): ...

写成 ``Any`` 的话，``_to_provider_input`` 产出什么、``score`` 收什么就完全
失去关联，传错了要到发请求时才知道。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Protocol, cast, final

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


class RerankerPort[ProviderInput](ABC):
    """应用层可使用的重排契约。

    ``rank/arank`` 是面向使用者的入口；``score/ascore`` 保留为兼容的低层
    接口，并由结构化入口复用。
    """

    _gate: _AsyncGate | None = None

    def bind_gate(self, gate: _AsyncGate | None) -> None:
        self._gate = gate

    @final
    async def ascore(
        self,
        query: ProviderInput,
        documents: Sequence[ProviderInput],
        **kwargs: Any,
    ) -> list[float]:
        if self._gate is None:
            return await self._ascore(query, documents, **kwargs)
        async with self._gate:
            return await self._ascore(query, documents, **kwargs)

    @abstractmethod
    async def _ascore(
        self,
        query: ProviderInput,
        documents: Sequence[ProviderInput],
        **kwargs: Any,
    ) -> list[float]: ...

    @abstractmethod
    def score(
        self,
        query: ProviderInput,
        documents: Sequence[ProviderInput],
        **kwargs: Any,
    ) -> list[float]:
        """兼容的同步低层打分接口。

        入参已经是供应商格式（由 ``_to_provider_input`` 翻译），所以类型随
        适配器而定。
        """

    def _to_provider_input(self, content: ContentInput) -> ProviderInput:
        """把共享内容类型转换成供应商输入；纯文本模型使用默认实现。

        默认实现产出 ``str``，因此只对 ``ProviderInput`` 含 ``str`` 的适配器
        成立（``RerankerPort[str]`` 与 Qwen 的
        ``RerankerPort[str | ScoreMultiModalParam]`` 都满足）。把
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


#: 兼容别名。未参数化时等价于 ``RerankerPort[Any]`` —— 新代码请显式写出
#: 供应商输入类型，例如 ``RerankerPort[str]``。
BaseReranker = RerankerPort

__all__ = ["BaseReranker", "RerankerPort"]
