from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence


async def map_concurrently[Item, Result](
    operation: Callable[[Item], Awaitable[Result]],
    items: Sequence[Item],
    *,
    limit: int,
) -> list[Result]:
    """以固定数量的 worker 保序处理一批输入；失败和取消都先排干兄弟任务。

    调用方返回后即可释放整批使用的文件或客户端。排干期间的重复取消不得
    再次取消 worker，否则会打断其 finally；最终保留最初触发退出的异常。
    """
    if limit <= 0:
        raise ValueError(f"limit 必须大于 0，收到 {limit}")
    if not items:
        return []

    pending = iter(enumerate(items))
    results: dict[int, Result] = {}
    stopping = False

    async def worker() -> None:
        # next 与取走序号之间没有 await；同一事件循环中的 worker 不会重复领取。
        for index, item in pending:
            if stopping:
                return
            results[index] = await operation(item)

    workers = [asyncio.create_task(worker()) for _ in range(min(limit, len(items)))]
    completion = asyncio.gather(*workers)
    try:
        # 取消由本函数统一发一次；直接 await gather 会把后续取消重复转发给 worker。
        await asyncio.shield(completion)
    except BaseException:
        # 即使某项自行吞掉取消并返回，也不应继续启动这批的后续请求。
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
        # 请求取消可能早于 completion 报错；取回异常，避免遗留未观察的 future。
        if not completion.cancelled():
            completion.exception()
        raise
    return [results[index] for index in range(len(items))]
