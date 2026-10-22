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
    """同步模型边界；资源关闭也在调用线程完成。"""

    #: 声明单请求容量；调度方决定分批方式与并发数。
    batch_limit: int

    def embed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """生成检索查询向量。"""
        ...

    def embed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """生成单篇待检索文档的向量。"""
        ...

    def embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """**恰好一次往返**，返回与输入等长、同序的向量列表。

        调用方必须保证 ``len(documents) <= batch_limit``。
        """
        ...

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用，业务代码不碰。"""
        ...

    def close(self) -> None: ...


@runtime_checkable
class AsyncEmbeddingPort(Protocol):
    """异步模型边界；网络调用与资源关闭都可等待。"""

    #: 声明单请求容量；调度方决定分批方式与并发数。
    batch_limit: int

    async def embed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """异步生成检索查询向量。"""
        ...

    async def embed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """异步生成单篇待检索文档的向量。"""
        ...

    async def embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """``embed_batch`` 的异步版本；同样是恰好一次往返。"""
        ...

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用，业务代码不碰。"""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class MultimodalEmbeddingPort(Protocol):
    """同步图片与图文混合输入能力。"""

    def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]: ...


@runtime_checkable
class AsyncMultimodalEmbeddingPort(Protocol):
    """异步图片与图文混合输入能力。"""

    async def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]: ...


__all__ = [
    "EmbeddingPort",
    "AsyncEmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
    "AsyncMultimodalEmbeddingPort",
]
