"""业务层使用的 Reranker 契约。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from comet_rag.ports.content import ContentInput, RankedDocument, RerankDocument
from comet_rag.ports.gate import AsyncGate


@runtime_checkable
class RerankerPort(Protocol):
    """同步模型边界；资源关闭也在调用线程完成。"""

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

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用，业务代码不碰。"""
        ...

    def close(self) -> None: ...


@runtime_checkable
class AsyncRerankerPort(Protocol):
    """异步模型边界；网络调用与资源关闭都可等待。"""

    async def rank(
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

    async def close(self) -> None: ...


__all__ = ["RerankerPort", "AsyncRerankerPort"]
