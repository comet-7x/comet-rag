"""进程级并发闸门的结构类型。

放在 `ports/` 而不是 `core/concurrency.py` 旁边，是因为**依赖方向**：
`core` 是组合根，它 import ports；反过来不行。Port 需要在 ``bind_gate``
的签名里说出"我接受什么"，所以这个形状必须由被依赖的一侧定义。

写成 Protocol 而非 import 具体的 ``Gate``：闸门的实现是部署关切（可以是
信号量、令牌桶、或测试里的计数器），契约只关心"能不能 async with"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AsyncGate(Protocol):
    """限流器：既能 `async with`，也能 `with`。

    名字保留 `AsyncGate` 是为了不动一大片调用方；实际契约是**两个入口共用
    同一份预算**。同步那半是可选的 —— 只有异步入口的实现仍然满足这个
    Protocol（Python 的结构类型不检查缺失的可选成员），`GatedResource` 会
    在同步侧先探测再使用。
    """

    async def __aenter__(self) -> Any: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class SyncGate(Protocol):
    """限流器的同步入口。与 `AsyncGate` 是同一个对象的两副面孔。"""

    def __enter__(self) -> Any: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


__all__ = ["AsyncGate", "GatedResource", "SyncGate"]


class GatedResource:
    """给一个会打外部服务的资源接上进程级闸门。

    ## 为什么这段实现住在 `ports/`

    `ports/` 本来只放契约。破例的理由是**分层**：`engines/`（loader）与
    `infrastructure/`（模型适配器）都需要它，而按守卫规则 engines 只能 import
    `engines` 和 `ports` —— `ports/` 是两者唯一的共同下游。

    另一条路是各写一份。但"闸门怎么进"正是不能有两份的东西：写岔了不会报错，
    只会让其中一条路悄悄不限流。宁可让 `ports/` 多这十几行。

    ## 为什么闸门默认是 `None`

    没挂闸门就是不限流。这让"当库用"的场景（单测、脚本、把 Comet-RAG 当依赖
    引入）不必先装配一套限流器才能跑一次加载或嵌入。

    代价是闸门属于**静默失效**型保护：忘了挂不会报错，只是限流不生效 ——
    本项目实测踩过一次"配置写 4、实际并发 128"。所以组合根挂完之后会用
    `_assert_gated` 复验，`tests/unit/test_layering.py` 也禁止在组合根之外
    直接 new 具体实现。防线在那两处，不在这里。
    """

    _gate: AsyncGate | None = None

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用。"""
        self._gate = gate

    def _through_gate_sync[T](self, call: Callable[[], T]) -> T:
        """在闸门内执行一次**同步**请求。

        同步路径此前完全不受闸门约束（#44）：`Pipeline.batch_run` 用
        `max_concurrency` 个线程跑来源，每个来源内部再开 `max_concurrency` 路，
        实测配置写 4、真实并发 16。

        闸门现在两侧共用一份预算，所以这里能直接 `with`。老的闸门实现只有
        异步入口，因此先探测再用 —— 注入进来的第三方限流器可能还是旧形状，
        那种情况下退化成不限流，与"没挂闸门"一致，而不是当场崩掉。
        """
        gate = self._gate
        if gate is None or not isinstance(gate, SyncGate):
            return call()
        with gate:
            return call()

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
