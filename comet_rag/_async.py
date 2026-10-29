from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence


async def run_sync[**P, R](
    function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    """线程不能强制取消；排干后才允许调用方释放文件和客户端。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break
    if cancelled is not None:
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


async def map_concurrently[T, R](
    operation: Callable[[T], Awaitable[R]], items: Sequence[T], *, limit: int
) -> list[R]:
    """固定数量的 worker 保序处理；失败停止领取并排干已启动调用。"""
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if not items:
        return []
    pending = iter(enumerate(items))
    results: dict[int, R] = {}
    stopping = False

    async def worker() -> None:
        nonlocal stopping
        for index, item in pending:
            if stopping:
                return
            try:
                results[index] = await operation(item)
            except BaseException:
                stopping = True
                raise

    workers = [asyncio.create_task(worker()) for _ in range(min(limit, len(items)))]
    completion = asyncio.gather(*workers)
    try:
        await asyncio.shield(completion)
    except BaseException:
        stopping = True
        for task in workers:
            if not task.done():
                task.cancel()
        drained = asyncio.gather(*workers, return_exceptions=True)
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                continue
        if not completion.cancelled():
            completion.exception()
        raise
    return [results[index] for index in range(len(items))]
