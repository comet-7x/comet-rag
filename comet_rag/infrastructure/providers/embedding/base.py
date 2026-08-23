"""Embedding 适配器基类：把供应商差异收敛成两个扩展点。

契约在 :mod:`comet_rag.ports.embedding`；本模块是**可选的**
实现复用 —— 满足 ``EmbeddingPort`` 形状的对象不必继承它。

## 适配器只需要填两个洞

``embed``（同步）与 ``_aembed``（异步）。其余全部由本类合成：查询/文档的
语义区分、闸门。多模态适配器再覆写 ``embed_media``/``_aembed_media``；
支持服务端原生批量的适配器再抬高 ``batch_limit`` 并覆写 ``_embed_batch``。

## 模板方法：``aembed`` 是 ``final``，``_aembed`` 才是扩展点

闸门必须在**每一条真实请求**外面。如果扩展点就是 ``aembed`` 本身，子类
一覆写就把闸门覆写掉了 —— 而且不报错。拆成"final 的外壳 + abstract 的内核"，
子类在类型层面就没有绕过闸门的写法。

## 这里不排程

"发几个请求、几个并发"是调用方的事，代码在
:mod:`comet_rag.engines.embedding.batch`。本模块只回答两件事：
**我一次最多能吃几篇**（``batch_limit``），以及**把这一块发出去**
（``embed_batch``，恰好一次往返）。

线程池、信号量、``max_concurrency`` 都不在这里 —— 模型不该替调用方决定
要不要起线程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, final

from comet_rag.infrastructure.providers.base import GatedModel
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

        这里曾经就是抽象方法本身，于是 `model.embed(...)` 直接调用完全绕开
        预算：实测闸门 limit=2 时真实峰值 8（评审指出）。给上面几个带语义的
        入口加闸而漏掉它们脚下这个公开入口，等于前门上锁、后门敞着。
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
        return await self._through_gate(lambda: self._aembed_batch(documents, **options))

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
    """图片与混合内容的向量化能力。

    ## 为什么是 mixin，而不是基类上的一个"默认抛异常"的方法

    ``MultimodalEmbeddingPort`` 是 ``@runtime_checkable`` 的，意思是调用方
    可以用 ``isinstance`` 问"这个模型能不能吃图片"。而 Protocol 的
    ``isinstance`` **只看方法在不在**，不看它做什么 —— 只要基类给了个
    "默认抛 TypeError"的 ``embed_media``，纯文本的 OpenAI 适配器就同样
    通过检查，协议当场变成谎话。

    做成 mixin，能力就是**结构性**的：不继承就真的没有这个方法，
    ``isinstance`` 的答案与运行时行为永远一致。

    ## 与文本入口分开也是刻意的

    两者输入域不同，硬合成一个就只能标 ``Any``，于是"这个参数能传什么"
    重新变成运行时才知道的事 —— 那正是本次重构要消除的东西。
    """

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
        """同步多模态入口。与 `aembed_media` 一样受闸门保护。

        修 #44 之前这里是个可覆写的抽象方法，Qwen 的实现直接调未加闸的
        `embed` —— 于是同步图片请求整条路绕开了预算，而图片恰恰比文本重得多
        （评审指出）。
        """
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
