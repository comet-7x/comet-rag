"""Reranker Port：业务代码对重排能力的全部要求。

## 第 1 步加的类型参数，在这里消失了

上一步为了消灭 ``score(query: Any, documents: Any)`` 里的 ``Any``，把
``RerankerPort`` 参数化成了 ``RerankerPort[ProviderInput]`` —— 因为
``score``/``ascore`` 收的是**已翻译成供应商格式**的东西（文本模型是 ``str``，
Qwen 多模态是 ``str | ScoreMultiModalParam``）。

把实现搬走之后才看清楚：``score``/``ascore`` 从来就不是应用层要的东西。
业务代码只调 ``rank``/``arank``，那两个入口全程是本项目自己的类型
（``ContentInput`` 进、``RankedDocument`` 出），跟供应商格式毫无关系。

所以类型参数属于**基类**而不是 Port：``BaseReranker[ProviderInput]`` 保留它，
``RerankerPort`` 一个都不需要。`RetrievalService` 于是可以直接写
``reranker: RerankerPort``，不必退化成 ``RerankerPort[Any]``。

这也说明第 1 步和第 2 步分开做是对的：混在一起时只会看到"要么 Any 要么
类型参数"这两个选项，看不见"这个方法根本不该在契约里"这第三个。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from comet_rag.application.ports.gate import AsyncGate
from comet_rag.models.content import ContentInput, RankedDocument, RerankDocument


@runtime_checkable
class RerankerPort(Protocol):
    """应用层可使用的重排契约。"""

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
        ...

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
        ...

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用，业务代码不碰。"""
        ...

    async def aclose(self) -> None:
        """释放适配器资源。"""
        ...


__all__ = ["RerankerPort"]
