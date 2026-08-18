"""进程级并发闸门的结构类型。

放在 `ports/` 而不是 `core/concurrency.py` 旁边，是因为**依赖方向**：
`core` 是组合根，它 import ports；反过来不行。Port 需要在 ``bind_gate``
的签名里说出"我接受什么"，所以这个形状必须由被依赖的一侧定义。

写成 Protocol 而非 import 具体的 ``Gate``：闸门的实现是部署关切（可以是
信号量、令牌桶、或测试里的计数器），契约只关心"能不能 async with"。
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AsyncGate(Protocol):
    """任何可作为异步上下文管理器使用的限流器。"""

    async def __aenter__(self) -> Any: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


__all__ = ["AsyncGate"]
