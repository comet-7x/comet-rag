"""Embedding Port：业务代码对向量化能力的全部要求。

## 这里只有契约，没有实现

本模块曾经同时是契约和基类，装着 ``ThreadPoolExecutor``、``asyncio.Semaphore``
和一整套模板方法。那些是**适配器怎么把请求发出去**的问题 —— 扇出多宽、
同步路径开几个线程、闸门在哪一层拿 —— 全是部署关切，不是应用层的要求。
它们现在住在 :mod:`comet_rag.infrastructure.models.base` 及各自的
``base.py`` 里。

分开之后契约小得能一眼看完，而这正是它该有的样子：`RetrievalService`
只需要 ``aembed_query``，`IngestRunner` 和 pipeline 只需要
``aembed_documents``/``embed_documents``。多出来的每一个方法都是在替
使用者做他们没要求过的决定。

## 为什么是 Protocol 而不是 ABC

Port 表达的是"我需要什么"，不是"你要继承谁"。写成 Protocol，任何形状
对得上的对象都能注入 —— 测试里的假模型、用户自己接的第三方服务，都不必
先 import 本项目的基类。仓库内的适配器仍然继承
``BaseEmbeddingModel``，但那是为了复用实现，不是为了满足契约。

契约是否真被满足由 `core/bootstrap.py` 的返回类型静态钉住：
``build_embedding_model() -> EmbeddingPort`` 返回具体适配器，形状对不上
pyright 当场报错。

## ``bind_gate`` 为什么在契约里

它是唯一一个业务代码不调、却仍属于契约的方法：**注入进来的实现必须能被
限流**。组合根挂上闸门后还会用 ``_assert_gated`` 复验（见 bootstrap），
因为闸门是"没挂上也不报错"的静默失效型保护。把它写进 Port，是让这条义务
在类型层面就说出口，而不是等运行时才发现。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from comet_rag.application.ports.gate import AsyncGate
from comet_rag.models.content import ContentInput, MediaResource

#: 单次批量调用的默认扇出宽度。它约束的是**一次调用内**的并发，
#: 进程级总量由闸门另行控制，两者是不同的旋钮。
DEFAULT_MODEL_BATCH_CONCURRENCY = 16


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

    def embed_documents(
        self,
        documents: Sequence[str],
        /,
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[list[float]]:
        """批量生成文档向量，结果顺序与输入一致。"""
        ...

    async def aembed_documents(
        self,
        documents: Sequence[str],
        /,
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[list[float]]:
        """异步批量生成文档向量，结果顺序与输入一致。"""
        ...

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用，业务代码不碰。"""
        ...

    async def aclose(self) -> None:
        """释放适配器资源。"""
        ...


@runtime_checkable
class MultimodalEmbeddingPort(Protocol):
    """具备图片与混合内容能力的可选扩展。

    独立于 ``EmbeddingPort``：多模态是**能力**而非要求，纯文本适配器
    （如 OpenAI）不该被迫实现它。需要图片向量的调用方用
    ``isinstance(model, MultimodalEmbeddingPort)`` 判定。

    这个 ``isinstance`` 要有意义，实现侧就不能给纯文本模型留一个"默认抛
    异常"的 ``embed_media`` —— Protocol 的运行时检查只看方法在不在。
    所以能力做成 mixin（``MultimodalEmbeddingMixin``），不继承就真的没有
    这个方法。

    只声明 ``embed_media``/``aembed_media`` 两个入口：``MediaResource``
    （单张图片）与 ``ContentInput``（图文混排）都由它们收，不必为每种
    输入各开一个方法。适配器自己的便捷包装（Qwen 的 ``embed_image`` 等）
    不属于契约。
    """

    def embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]: ...

    async def aembed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]: ...


__all__ = [
    "DEFAULT_MODEL_BATCH_CONCURRENCY",
    "EmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
]
