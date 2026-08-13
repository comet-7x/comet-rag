"""重排模型的抽象基类。

闸门的道理与 `embedding/base.py` 完全一样，且**共用同一个 `Gate` 实例** ——
重排与向量化抢的是同一块 GPU，各自限流等于没限：两边都配 8，服务端看到的
就是 16。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from comet_rag.core.concurrency import Gate


class BaseReranker(ABC):
    #: 与 embedding 模型共用的进程级闸门，由组合根注入。
    _gate: Gate | None = None

    def bind_gate(self, gate: Gate | None) -> None:
        self._gate = gate

    async def ascore(self, query: Any, documents: Any, **kwargs: Any) -> list[float]:
        """打分。**不要覆写它**，要改行为请实现 `_ascore`。"""
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
        """同步版。**不经闸门**（`Gate` 管不了线程池），仅供"当库用"的场景。"""
