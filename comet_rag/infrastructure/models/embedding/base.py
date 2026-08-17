"""Embedding 适配器基类：把供应商差异收敛成两个扩展点。

契约在 :mod:`comet_rag.application.ports.embedding`；本模块是**可选的**
实现复用 —— 满足 ``EmbeddingPort`` 形状的对象不必继承它。

## 适配器只需要填两个洞

``embed``（同步）与 ``_aembed``（异步）。其余全部由本类合成：查询/文档的
语义区分、批量扇出、闸门。多模态适配器再覆写 ``embed_media``/``_aembed_media``。

## 模板方法：``aembed`` 是 ``final``，``_aembed`` 才是扩展点

闸门必须在**每一条真实请求**外面。如果扩展点就是 ``aembed`` 本身，子类
一覆写就把闸门覆写掉了 —— 而且不报错。拆成"final 的外壳 + abstract 的内核"，
子类在类型层面就没有绕过闸门的写法。

## 批量方法只管一次调用的扇出宽度

``max_concurrency`` 限的是单次 ``aembed_documents`` 内部同时在飞的请求数，
闸门限的是整个进程。两者叠加：32 个 worker 各开 4 路，进程级仍然卡在闸门
配置的上限，而不是 128。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, final

from comet_rag.application.ports.embedding import (
    DEFAULT_MODEL_BATCH_CONCURRENCY,
    EmbeddingPort,
    EmbeddingTask,
    MultimodalEmbeddingPort,
)
from comet_rag.infrastructure.models.base import GatedModel
from comet_rag.models.content import ContentInput, MediaResource


class BaseEmbeddingModel(GatedModel, ABC):
    """Embedding 适配器的共享实现。"""

    #: 供应商是否支持"一个请求装多条文档"。为 False 时批量入口退化为
    #: 扇出 N 个单条请求 —— 能用，但每条都要付一次往返。
    _native_document_batch = False

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
    def embed(self, data: str, /, **kwargs: Any) -> list[float]:
        """同步底层入口；同步调用不经过 asyncio 闸门。"""

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
        return self.embed(query, **self._options_for(EmbeddingTask.QUERY, kwargs))

    @final
    async def aembed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """异步生成检索查询向量。"""
        return await self.aembed(
            query, **self._options_for(EmbeddingTask.QUERY, kwargs)
        )

    @final
    def embed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """生成单篇待检索文档的向量。"""
        return self.embed(
            document, **self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        )

    @final
    async def aembed_document(self, document: str, /, **kwargs: Any) -> list[float]:
        """异步生成单篇待检索文档的向量。"""
        return await self.aembed(
            document, **self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        )

    # ── 批量 ───────────────────────────────────────────────────────────────

    def _embed_documents_native(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        raise NotImplementedError

    async def _aembed_documents_native(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        raise NotImplementedError

    @final
    def embed_documents(
        self,
        documents: Sequence[str],
        /,
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[list[float]]:
        """批量生成文档向量，结果顺序与输入一致。"""
        self._validate_max_concurrency(max_concurrency)
        if not documents:
            return []
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        if self._native_document_batch:
            return self._embed_documents_native(documents, **options)
        return self.batch_embed(
            list(documents), max_concurrency=max_concurrency, **options
        )

    @final
    async def aembed_documents(
        self,
        documents: Sequence[str],
        /,
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[list[float]]:
        """异步批量生成文档向量，结果顺序与输入一致。"""
        self._validate_max_concurrency(max_concurrency)
        if not documents:
            return []
        options = self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        if not self._native_document_batch:
            return await self.abatch_embed(
                list(documents), max_concurrency=max_concurrency, **options
            )
        # 原生批量是**一个**请求，所以整体占一个闸门名额；扇出那条路则由
        # 每次 aembed 各自过闸，两边都不会把限流算漏。
        return await self._through_gate(
            lambda: self._aembed_documents_native(documents, **options)
        )

    def batch_embed(
        self,
        texts: Sequence[str],
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[list[float]]:
        """同步批量入口。结果顺序与输入一致。"""
        self._validate_max_concurrency(max_concurrency)
        embedding_data_list = list(texts)
        if not embedding_data_list:
            return []

        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        max_workers = min(max_concurrency, len(embedding_data_list))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self.embed, data, **kwargs)
                for data in embedding_data_list
            ]
            return [future.result() for future in futures]

    async def abatch_embed(
        self,
        texts: Sequence[str],
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[list[float]]:
        """异步批量入口；每条请求都通过 ``aembed`` 的全局闸门。

        结果顺序与输入一致 —— ``asyncio.gather`` 保证这一点，调用方不必自己
        按索引回填。
        """
        self._validate_max_concurrency(max_concurrency)
        embedding_data_list = list(texts)
        if not embedding_data_list:
            return []

        semaphore = asyncio.Semaphore(min(max_concurrency, len(embedding_data_list)))

        async def _limited(data: str) -> list[float]:
            async with semaphore:
                return await self.aembed(data, **kwargs)

        return await asyncio.gather(*[_limited(data) for data in embedding_data_list])

    @staticmethod
    def _validate_max_concurrency(max_concurrency: int) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency 必须大于 0，收到 {max_concurrency}")

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
    def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """同步多模态入口；同步路径不经过 asyncio 闸门。"""

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
    "DEFAULT_MODEL_BATCH_CONCURRENCY",
    "EmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
]
