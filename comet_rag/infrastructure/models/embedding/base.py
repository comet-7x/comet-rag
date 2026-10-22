"""Embedding 适配器模板：统一任务语义、批量能力与并发闸门。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, final

from comet_rag.infrastructure.models.base import GatedModel
from comet_rag.ports.content import ContentInput, MediaResource
from comet_rag.ports.embedding import (
    AsyncEmbeddingPort,
    AsyncMultimodalEmbeddingPort,
    EmbeddingPort,
    EmbeddingTask,
    MultimodalEmbeddingPort,
)


class _EmbeddingSemantics:
    """同步、异步模板共用的任务语义与批量能力约束。"""

    #: 单请求的文档容量；大于 1 时必须实现原生 _embed_batch。
    batch_limit: int = 1

    def _task_options(self, task: EmbeddingTask) -> Mapping[str, Any]:
        """将通用任务语义映射为供应商参数；普通模型无需覆写。"""
        return {}

    def _options_for(
        self, task: EmbeddingTask, options: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {**self._task_options(task), **options}

    def _require_native_batch(self, documents: Sequence[str]) -> None:
        if len(documents) > 1:
            raise NotImplementedError(
                f"{type(self).__name__} 声明 batch_limit={self.batch_limit}，"
                f"却没有实现 _embed_batch（收到 {len(documents)} 篇）。"
                "请覆写它，或把 batch_limit 保持为 1。"
            )


class BaseEmbeddingModel(_EmbeddingSemantics, GatedModel, ABC):
    """同步 Embedding 模板；公开请求入口统一获取进程级闸门。"""

    @abstractmethod
    def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        """适配器真正执行同步请求的地方。"""

    @final
    def embed(self, data: str, /, **kwargs: Any) -> list[float]:
        """同步底层入口。受闸门保护，不可覆写。

        所有公开入口必须经模板方法取闸门；否则直接调用 `model.embed(...)`
        会绕过进程级预算。
        """
        return self._through_gate_sync(lambda: self._embed(data, **kwargs))

    @final
    def embed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """生成检索查询向量。"""
        options = self._options_for(EmbeddingTask.QUERY, kwargs)
        return self._through_gate_sync(lambda: self._embed(query, **options))

    @final
    def embed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """生成单篇待检索文档的向量。"""
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        return self._through_gate_sync(lambda: self._embed(document, **options))

    @final
    def embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """恰好一次往返，返回与输入等长、同序的向量。

        整批只占一个闸门名额；同步和异步模型可绑定同一份预算。
        """
        if not documents:
            return []
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        return self._through_gate_sync(lambda: self._embed_batch(documents, **options))

    def _embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """默认实现只处理"批量大小为 1"，即 ``batch_limit`` 保持默认的情形。"""
        self._require_native_batch(documents)
        # 闸门已由 `embed_batch` 持有，这里调未加闸的 `_embed`
        return [self._embed(documents[0], **kwargs)]

    def close(self) -> None:
        """无资源适配器沿用空操作。"""


class AsyncBaseEmbeddingModel(_EmbeddingSemantics, GatedModel, ABC):
    """异步 Embedding 模板；不提供隐藏事件循环的同步包装。"""

    @abstractmethod
    async def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        """适配器真正执行异步请求的地方。

        入参声明为 ``str`` 而非 ``Any``：契约存在的意义就是让调用方不必猜
        "这个字符串到底是文本、路径还是 base64"。子类**可以放宽**（逆变，
        例如 Qwen 额外接受 ``MediaResource``），但不能收窄。

        返回值统一是 ``list[float]``。供应商侧 base64 之类的传输优化必须在
        适配器内部解回浮点数组：那是线路格式，不该泄漏给调用方。
        """

    @final
    async def embed(self, data: str, /, **kwargs: Any) -> list[float]:
        """异步底层入口。受闸门保护，不可覆写。"""
        return await self._through_gate(lambda: self._embed(data, **kwargs))

    @final
    async def embed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """异步生成检索查询向量。"""
        return await self.embed(query, **self._options_for(EmbeddingTask.QUERY, kwargs))

    @final
    async def embed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """异步生成单篇待检索文档的向量。"""
        return await self.embed(
            document, **self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        )

    @final
    async def embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """``embed_batch`` 的异步版本。

        整块占**一个**闸门名额，因为它就是一个请求 —— 无论装了 1 篇还是 512 篇。
        """
        if not documents:
            return []
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        return await self._through_gate(lambda: self._embed_batch(documents, **options))

    async def _embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        # 调未加闸的 `_embed`：闸门已由 `embed_batch` 持有，再走一次会自锁。
        self._require_native_batch(documents)
        return [await self._embed(documents[0], **kwargs)]

    async def close(self) -> None:
        """无资源适配器沿用空操作。"""


class MultimodalEmbeddingMixin(GatedModel, ABC):
    """同步多模态能力；公开入口获取共享闸门。"""

    @abstractmethod
    def _embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """多模态适配器真正执行同步请求的地方。"""

    @final
    def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """受共享闸门保护的同步多模态入口。"""
        return self._through_gate_sync(lambda: self._embed_media(data, **kwargs))


class AsyncMultimodalEmbeddingMixin(GatedModel, ABC):
    """异步多模态能力；公开入口获取共享闸门。"""

    @abstractmethod
    async def _embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """多模态适配器真正执行异步请求的地方。"""

    @final
    async def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """异步多模态入口。与 ``embed`` 一样受闸门保护 —— 图片请求通常比
        文本更重（一张图能顶几十倍 token），绕开闸门等于给限流开个后门。"""
        return await self._through_gate(lambda: self._embed_media(data, **kwargs))


__all__ = [
    "BaseEmbeddingModel",
    "AsyncBaseEmbeddingModel",
    "MultimodalEmbeddingMixin",
    "AsyncMultimodalEmbeddingMixin",
    "EmbeddingPort",
    "AsyncEmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
    "AsyncMultimodalEmbeddingPort",
]
