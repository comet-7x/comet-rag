from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arq.worker import Worker


async def stop_workers(
    polling: Sequence[asyncio.Task[None]], workers: Sequence[Worker]
) -> None:
    """非 burst 测试 worker：停轮询、取消业务、排干传输，随后仍由 SDK close 关闭连接。"""
    for task in polling:
        task.cancel()
    polling_results = await asyncio.gather(*polling, return_exceptions=True)
    # run_job 还负责 Redis 确认，不能把它与函数本体 job_tasks 一起取消。
    for worker in workers:
        for job in tuple(worker.job_tasks.values()):
            job.cancel()
    transports = [task for worker in workers for task in worker.tasks.values()]
    results = await asyncio.gather(*transports, return_exceptions=True)
    for worker in workers:
        # 已消费结果后再解除引用；Worker.close 会再次 gather 此表，取消结果会阻断关闭。
        for key, task in tuple(worker.tasks.items()):
            if task.done():
                del worker.tasks[key]
    errors = [
        result
        for result in (*polling_results, *results)
        if isinstance(result, BaseException)
        and not isinstance(result, asyncio.CancelledError)
    ]
    if errors:
        raise BaseExceptionGroup("worker 测试清理期间的未处理异常", errors)
