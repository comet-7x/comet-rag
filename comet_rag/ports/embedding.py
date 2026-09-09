"""业务层使用的文本与多模态 Embedding 契约。"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from comet_rag.ports.content import ContentInput, MediaResource
from comet_rag.ports.gate import AsyncGate


class EmbeddingTask(StrEnum):
    """文本向量的使用语义；适配器可据此选择编码器或提示词。

    留在 Port 这一侧是有意的：它正是 ``embed_query`` 与 ``embed_document``
    分成两个方法的理由 —— 同一段文字当查询和当文档编码，向量可以不同。
    """

    QUERY = "query"
    DOCUMENT = "document"


@runtime_checkable
class EmbeddingPort(Protocol):
    """应用层可使用的 Embedding 契约。"""

    #: 一次请求最多能装多少篇文档。``1`` 表示服务端不支持批量，只能一条一发。
    #:
    #: 这是模型**声明能力**，不是模型**决定策略**：它只回答"我一次最多能吃
    #: 几个"，至于要不要装满、几个请求并发发出去，由调度方决定。
    batch_limit: int

    def embed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """生成检索查询向量。"""
        ...

    async def aembed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """异步生成检索查询向量。"""
        ...

    def embed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """生成单篇待检索文档的向量。"""
        ...

    async def aembed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """异步生成单篇待检索文档的向量。"""
        ...

    def embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """**恰好一次往返**，返回与输入等长、同序的向量列表。

        调用方必须保证 ``len(documents) <= batch_limit``。
        """
        ...

    async def aembed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """``embed_batch`` 的异步版本；同样是恰好一次往返。"""
        ...

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用，业务代码不碰。"""
        ...

    async def aclose(self) -> None:
        """释放适配器资源。"""
        ...


@runtime_checkable
class MultimodalEmbeddingPort(Protocol):
    """图片与图文混合输入的可选结构性能力。"""

    def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]: ...

    async def aembed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]: ...


__all__ = [
    "EmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
]
