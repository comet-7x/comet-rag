"""Embedding Port：区分查询、文档与多模态输入。

公共方法参考 LlamaIndex/LangChain 的语义划分。旧的 ``embed/aembed`` 仍是
供应商适配器的底层扩展点，方便已有自定义实现平滑迁移；业务代码应优先调用
``embed_query``、``embed_documents`` 等有明确意图的方法。

异步单条调用继续使用模板方法守住进程级闸门：子类实现 ``_aembed``，不能
覆写 ``aembed``。批量方法只负责单次调用的扇出宽度，每一条真实请求仍会经过
同一个进程级闸门。

## 文本入口只收 ``str``

底层扩展点 ``embed``/``_aembed`` 的入参**不是** ``Any``：契约存在的意义就是
让调用方不必猜"这个字符串到底是文本、路径还是 base64"。多模态输入走
``MultimodalEmbeddingPort``，用 ``MediaResource``/``ContentInput`` 明确表达。

子类**可以放宽**入参（逆变，例如 Qwen 额外接受 ``MediaResource``），但不能
收窄 —— 纯文本适配器（如 OpenAI）恰好与本签名一致。

返回值统一是 ``list[float]``。供应商侧的 base64 之类的传输优化必须在适配器
内部解码回浮点数组：那是线路格式，不该泄漏给调用方。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, final, runtime_checkable

from comet_rag.models.content import ContentInput, MediaResource

DEFAULT_MODEL_BATCH_CONCURRENCY = 16


class _AsyncGate(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class EmbeddingTask(StrEnum):
    """文本向量的使用语义；适配器可据此选择编码器或提示词。"""

    QUERY = "query"
    DOCUMENT = "document"


class EmbeddingPort(ABC):
    """应用层可使用的 Embedding 契约。"""

    _gate: _AsyncGate | None = None
    _native_document_batch = False

    def bind_gate(self, gate: _AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用。"""
        self._gate = gate

    @final
    async def aembed(self, data: str, **kwargs: Any) -> list[float]:
        """兼容的异步底层入口；业务代码应调用带语义的方法。"""
        if self._gate is None:
            return await self._aembed(data, **kwargs)
        async with self._gate:
            return await self._aembed(data, **kwargs)

    @abstractmethod
    async def _aembed(self, data: str, /, **kwargs: Any) -> list[float]:
        """适配器真正执行异步请求的扩展点。

        入参声明为 ``str``：子类可以放宽（逆变）以接受多模态输入，但纯文本
        路径的契约必须是明确的。
        """

    @abstractmethod
    def embed(self, data: str, /, **kwargs: Any) -> list[float]:
        """兼容的同步底层入口；同步调用不经过 asyncio 闸门。"""

    # ── 多模态入口 ─────────────────────────────────────────────────────────
    #
    # 与文本入口**分开**是刻意的：两者输入域不同，硬合成一个就只能标 `Any`，
    # 于是"这个参数能传什么"重新变成运行时才知道的事 —— 那正是本次重构要
    # 消除的东西。默认实现明确拒绝，纯文本适配器什么都不用做。

    def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """同步多模态入口；同步路径不经过 asyncio 闸门。"""
        raise TypeError(f"{type(self).__name__} 只支持文本输入")

    @final
    async def aembed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """异步多模态入口。与 `aembed` 一样受进程级闸门保护 —— 图片请求
        通常比文本更重，绕开闸门等于把限流开了个后门。"""
        if self._gate is None:
            return await self._aembed_media(data, **kwargs)
        async with self._gate:
            return await self._aembed_media(data, **kwargs)

    async def _aembed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """多模态适配器的扩展点；默认不支持。"""
        raise TypeError(f"{type(self).__name__} 只支持文本输入")

    def _task_options(self, task: EmbeddingTask) -> Mapping[str, Any]:
        """将通用任务语义映射为供应商参数；普通模型无需覆写。"""
        return {}

    def _options_for(
        self, task: EmbeddingTask, options: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {**self._task_options(task), **options}

    @final
    def embed_query(self, query: str, /, **kwargs: Any) -> list[float]:
        """生成检索查询向量。"""
        return self.embed(
            query, **self._options_for(EmbeddingTask.QUERY, kwargs)
        )

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
    async def aembed_document(
        self, document: str, /, **kwargs: Any
    ) -> list[float]:
        """异步生成单篇待检索文档的向量。"""
        return await self.aembed(
            document, **self._options_for(EmbeddingTask.DOCUMENT, kwargs)
        )

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
        if self._gate is None:
            return await self._aembed_documents_native(documents, **options)
        async with self._gate:
            return await self._aembed_documents_native(documents, **options)

    def batch_embed(
        self,
        texts: Sequence[str],
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[list[float]]:
        """兼容的同步批量入口。结果顺序与输入一致。"""
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
        """兼容的异步批量入口；每条请求都通过 ``aembed`` 的全局闸门。

        结果顺序与输入一致 —— `asyncio.gather` 保证这一点，调用方不必自己
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


@runtime_checkable
class MultimodalEmbeddingPort(Protocol):
    """具备图片与混合内容能力的可选扩展。"""

    def embed_image(self, image: MediaResource, /, **kwargs: Any) -> list[float]: ...

    async def aembed_image(
        self, image: MediaResource, /, **kwargs: Any
    ) -> list[float]: ...

    def embed_content(self, content: ContentInput, /, **kwargs: Any) -> list[float]: ...

    async def aembed_content(
        self, content: ContentInput, /, **kwargs: Any
    ) -> list[float]: ...


# 兼容旧导入；新代码使用更能表达依赖倒置语义的 ``EmbeddingPort``。
BaseEmbeddingModel = EmbeddingPort

__all__ = [
    "BaseEmbeddingModel",
    "DEFAULT_MODEL_BATCH_CONCURRENCY",
    "EmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
]
