from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from arq import ArqRedis
from arq.worker import Worker

from comet_rag.infrastructure.task_execution.arq import ArqExecutor, run_task


def _make_worker(
    pool: ArqRedis, queue: str, executor: ArqExecutor, *, max_jobs: int
) -> Worker:
    return Worker(
        functions=[run_task],
        redis_pool=pool,
        queue_name=queue,
        max_jobs=max_jobs,
        poll_delay=0.02,
        handle_signals=False,
        ctx={"executor": executor},
        # 重试预算归 TaskStore；ARQ 再重试会造成两套 attempts 计数。
        retry_jobs=False,
        max_tries=1,
        keep_result=2,
        log_results=False,
    )


@contextlib.asynccontextmanager
async def _running(worker: Worker) -> AsyncIterator[Worker]:
    runner = asyncio.create_task(worker.async_run())
    try:
        yield worker
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        # 先停生产者，再收已起飞的任务；调用者最后关闭连接。
        for job in list(worker.tasks.values()):
            job.cancel()
        if worker.tasks:
            await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
