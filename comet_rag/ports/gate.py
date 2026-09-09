"""同步与异步共享并发预算的最小契约。"""

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
    """为跨层资源复用同一闸门外壳；未绑定时保持纯库模式可直接使用。"""

    _gate: AsyncGate | None = None

    def bind_gate(self, gate: AsyncGate | None) -> None:
        """绑定进程级并发闸门；由组合根调用。"""
        self._gate = gate

    def _through_gate_sync[T](self, call: Callable[[], T]) -> T:
        """同步入口与异步入口共享预算；旧式异步闸门退化为不限流。"""
        gate = self._gate
        if gate is None or not isinstance(gate, SyncGate):
            return call()
        with gate:
            return call()

    async def _through_gate[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """拿到许可后才创建协程，避免拒绝请求留下未等待的协程。"""
        if self._gate is None:
            return await call()
        async with self._gate:
            return await call()
