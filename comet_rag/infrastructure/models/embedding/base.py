"""Embedding 适配器模板：统一任务语义、批量能力与并发闸门。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, final

from comet_rag.infrastructure.models.base import GatedModel
from comet_rag.ports.content import ContentInput, MediaResource
from comet_rag.ports.embedding import (
    EmbeddingPort,
    EmbeddingTask,
    MultimodalEmbeddingPort,
)


class BaseEmbeddingModel(GatedModel, ABC):
    """Embedding 适配器的共享实现。"""

    #: 一次请求最多能装多少篇文档。默认 ``1`` = 服务端不支持批量。
    #: 抬高它就必须同时覆写 ``_embed_batch``/``_aembed_batch``，否则
    #: ``_require_native_batch`` 会拒绝。
    batch_limit: int = 1

    # ── 适配器必须实现的两个扩展点 ─────────────────────────────────────────

    @abstractmethod
    async def _aembed(self, data: str, /, **kwargs: Any) -> list[float]:
        """适配器真正执行异步请求的地方。

        入参声明为 ``str`` 而非 ``Any``：契约存在的意义就是让调用方不必猜
        "这个字符串到底是文本、路径还是 base64"。子类**可以放宽**（逆变，
        例如 Qwen 额外接受 ``MediaResource``），但不能收窄。

        返回值统一是 ``list[float]``。供应商侧 base64 之类的传输优化必须在
        适配器内部解回浮点数组：那是线路格式，不该泄漏给调用方。
        """

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
    async def aembed(self, data: str, **kwargs: Any) -> list[float]:
        """异步底层入口。受闸门保护，不可覆写。"""
        return await self._through_gate(lambda: self._aembed(data, **kwargs))

    # ── 任务语义 ───────────────────────────────────────────────────────────

    def _task_options(self, task: EmbeddingTask) -> Mapping[str, Any]:
        """将通用任务语义映射为供应商参数；普通模型无需覆写。"""
        return {}

    def _options_for(
        self, task: EmbeddingTask, options: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {**self._task_options(task), **options}

    # ── 带语义的公共入口 ───────────────────────────────────────────────────

    @final
    def embed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """生成检索查询向量。"""
        options = self._options_for(EmbeddingTask.QUERY, kwargs)
        return self._through_gate_sync(lambda: self._embed(query, **options))

    @final
    async def aembed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """异步生成检索查询向量。"""
        return await self.aembed(
            query, **self._options_for(EmbeddingTask.QUERY, kwargs)
        )

    @final
    def embed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """生成单篇待检索文档的向量。"""
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        return self._through_gate_sync(lambda: self._embed(document, **options))

    @final
    async def aembed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """异步生成单篇待检索文档的向量。"""
        return await self.aembed(
            document, **self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        )

    # ── 一次往返 ───────────────────────────────────────────────────────────

    @final
    def embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """恰好一次往返，返回与输入等长、同序的向量。

        与 `aembed_batch` 一样受闸门保护 —— 闸门现在两侧共用一份预算，同步
        路径不再是限流的后门（#44）。
        """
        if not documents:
            return []
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        return self._through_gate_sync(lambda: self._embed_batch(documents, **options))

    @final
    async def aembed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """``embed_batch`` 的异步版本。

        整块占**一个**闸门名额，因为它就是一个请求 —— 无论装了 1 篇还是 512 篇。
        """
        if not documents:
            return []
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        return await self._through_gate(
            lambda: self._aembed_batch(documents, **options)
        )

    def _embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """默认实现只处理"批量大小为 1"，即 ``batch_limit`` 保持默认的情形。"""
        self._require_native_batch(documents)
        # 闸门已由 `embed_batch` 持有，这里调未加闸的 `_embed`
        return [self._embed(documents[0], **kwargs)]

    async def _aembed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        # 调未加闸的 `_aembed`：闸门已由 `aembed_batch` 持有，再走一次会自锁。
        self._require_native_batch(documents)
        return [await self._aembed(documents[0], **kwargs)]

    def _require_native_batch(self, documents: Sequence[str]) -> None:
        """把"声明了批量能力却没实现"变成一句说得清的错误。

        默认实现一次只发一篇。如果子类把 ``batch_limit`` 调大却忘了覆写
        ``_embed_batch``/``_aembed_batch``，静默的后果是**在一个闸门名额里
        串行发 N 个请求** —— 限流数字还是对的，吞吐却掉到 1/N，而且没有任何
        迹象。所以这里直接拒绝。
        """
        if len(documents) > 1:
            raise NotImplementedError(
                f"{type(self).__name__} 声明 batch_limit={self.batch_limit}，"
                f"却没有实现 _embed_batch/_aembed_batch（收到 {len(documents)} 篇）。"
                f"请覆写它们，或把 batch_limit 保持为 1。"
            )

    async def aclose(self) -> None:
        """释放适配器资源；无资源实现沿用空操作。"""
        return None


class MultimodalEmbeddingMixin(GatedModel, ABC):
    """以结构性能力暴露多模态接口，使运行时 Protocol 检查可信。"""

    @abstractmethod
    async def _aembed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """多模态适配器真正执行异步请求的地方。"""

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

    @final
    async def aembed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """异步多模态入口。与 ``aembed`` 一样受闸门保护 —— 图片请求通常比
        文本更重（一张图能顶几十倍 token），绕开闸门等于给限流开个后门。"""
        return await self._through_gate(lambda: self._aembed_media(data, **kwargs))


__all__ = [
    "BaseEmbeddingModel",
    "MultimodalEmbeddingMixin",
    "EmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
]
