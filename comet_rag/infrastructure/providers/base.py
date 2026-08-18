"""模型适配器共用的实现细节。

这里放的是**所有外部模型服务都要面对的同一件事**：进程级限流。
Embedding 和 Reranker 打的是不同的接口、收的是不同的类型，但它们都在跟
同一个模型服务抢连接，所以共用一个闸门。

契约在 :mod:`comet_rag.ports`；本模块只解决"怎么做"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from comet_rag.ports.gate import AsyncGate


class GatedModel:
    """给适配器接上进程级并发闸门。

    ## 为什么闸门默认是 ``None``

    没挂闸门就是不限流。这让"当库用"的场景（单测、脚本、把 Comet-RAG
    当依赖引入的项目）不必先装配一套限流器才能跑一次 embed。

    代价是闸门属于**静默失效**型保护：忘了挂不会报错，只是限流不生效 ——
    本项目实测踩过一次"配置写 4、实际并发 128"。所以组合根挂完之后会用
    `_assert_gated` 复验，`tests/unit/test_layering.py` 也禁止在
    `core/bootstrap.py` 之外直接 new 具体模型。防线在那两处，不在这里。
    """

    _gate: AsyncGate | None = None

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用。"""
        self._gate = gate

    async def _through_gate[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """在闸门内执行一次真实请求。

        收的是**可调用对象**而不是已经建好的协程。闸门会拒绝请求（等待席位满
        或超时，抛 `Overloaded`），若协程在拿许可之前就建好，它便永远不会被
        await。实测那条 `RuntimeWarning: coroutine ... was never awaited` 是
        在 GC 时从 `asyncio/events.py` 里冒出来的 —— 既不指向真正的调用点，
        也压不住，过载时每拒一个请求就刷一条。

        延后到拿到许可之后再创建协程，就没有这个悬空对象。代价是每次调用多
        一个 lambda，相对一次网络往返可以忽略。
        """
        if self._gate is None:
            return await call()
        async with self._gate:
            return await call()


__all__ = ["GatedModel"]
