from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable

DEFAULT_QUERY_TIMEOUT_SECONDS = 30.0


class QueryDeadlineExceeded(TimeoutError):
    """服务共享预算耗尽；与供应商自身的 TimeoutError 区分。"""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"query deadline exceeded during {stage}")


def validate_query_timeout(seconds: float) -> None:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise ValueError("query timeout 必须是有限正数")


class QueryDeadline:
    """一次 search 共用单调时钟预算；取消后排干，不把超时伪装成硬关停。"""

    def __init__(self, seconds: float) -> None:
        validate_query_timeout(seconds)
        self._loop = asyncio.get_running_loop()
        self._expires_at = self._loop.time() + seconds

    async def run[T](self, operation: Callable[[], Awaitable[T]], *, stage: str) -> T:
        remaining = self._expires_at - self._loop.time()
        if remaining <= 0:
            # 不创建协程，也不启动已经没有预算的 IO。
            raise QueryDeadlineExceeded(stage)
        finished_at = self._expires_at

        async def invoke() -> T:
            nonlocal finished_at
            try:
                return await operation()
            finally:
                finished_at = self._loop.time()

        task = asyncio.create_task(invoke(), name=f"retrieval:{stage}:budget")
        try:
            done, _ = await asyncio.wait((task,), timeout=remaining)
            if done and finished_at < self._expires_at:
                return task.result()
        except BaseException:
            await _cancel_and_drain(task)
            raise

        # asyncio.wait 不取消工作；这里仅取消一次，以免重复取消打断 finally
        # 或同步线程桥的排干。吞掉取消、迟到的成功也不能变成有效结果。
        cancellation = await _cancel_and_drain(task)
        if cancellation is not None:
            raise cancellation
        if not task.cancelled() and task.exception() is not None:
            task.result()
        raise QueryDeadlineExceeded(stage)


async def _cancel_and_drain[T](task: asyncio.Task[T]) -> asyncio.CancelledError | None:
    if not task.done():
        task.cancel()
    drained = asyncio.gather(task, return_exceptions=True)
    cancellation = None
    while True:
        try:
            await asyncio.shield(drained)
            return cancellation
        except asyncio.CancelledError as exc:
            # 请求方在超时清理期间取消时，取消优先于超时/降级。
            cancellation = exc
