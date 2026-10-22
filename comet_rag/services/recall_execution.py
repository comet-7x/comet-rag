from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def run_recall_channels[T](
    *,
    dense: Callable[[], Awaitable[T]],
    keyword: Callable[[], Awaitable[T]],
    must_propagate: Callable[[BaseException], bool],
) -> tuple[T | Exception, T | Exception]:
    """并行执行两路召回；致命错误或取消时，排干兄弟任务后再传播原异常。"""

    async def run(channel: Callable[[], Awaitable[T]]) -> T | Exception:
        try:
            return await channel()
        except Exception as exc:
            if must_propagate(exc):
                raise
            return exc

    tasks = (
        asyncio.create_task(run(dense), name="retrieval:dense"),
        asyncio.create_task(run(keyword), name="retrieval:keyword"),
    )
    group = asyncio.gather(*tasks)
    try:
        # 由这里统一取消一次；调用方重复取消不能再次打断通道的 finally。
        results = await asyncio.shield(group)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        drained = asyncio.gather(*tasks, return_exceptions=True)
        while True:
            try:
                await asyncio.shield(drained)
                break
            except asyncio.CancelledError:
                continue
        # 调用方取消时 shield 不消费原 gather 的异常；排干后显式取回它。
        if not group.cancelled():
            group.exception()
        raise
    return results[0], results[1]
