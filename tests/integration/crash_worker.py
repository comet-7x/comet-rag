"""被 `kill -9` 的那个 worker 的进程入口（只给 `test_crash_recovery.py` 用）。

单独成一个模块，是因为它必须能被**另一个 Python 进程**导入：

    arq tests.integration.crash_worker.WorkerSettings

在测试进程里 cancel 一个协程**不算崩溃** —— `execute()` 会接住
`CancelledError` 并把任务干净地落成 CANCELLED，那正是我们要排除的路径。
真正要验的是"worker 死得连一个字都没写下"，只有 SIGKILL 一个真进程能做到。

`sweep=False`：回收由测试进程发起，免得这个 worker 把自己的僵尸任务回收了 ——
那样就测不出"另一个 worker 接管"了。
"""

from __future__ import annotations

from typing import Any

from comet_rag.tasks import (
    LANE_CPU,
    Done,
    StagePipeline,
    TaskContext,
    register,
    sleep_with_checkpoint,
)
from comet_rag.workers.base import WorkerProfile, build_settings

KIND = "crash-sleeper"

flow = StagePipeline()


@flow.stage("working", lane=LANE_CPU)
async def _working(ctx: TaskContext) -> Done:
    """第一次进来就长睡（等着被 kill），接管的那次直接完成。

    靠 `attempts` 分辨"这是第几次跑"：租约回收会把任务退回 PENDING，
    接管者进 RUNNING 时 attempts 变成 2。
    """
    task = await ctx.snapshot()
    if task.attempts >= 2:
        return Done(result={"attempts": task.attempts, "worker": ctx.worker_id})
    # 心跳间隔默认 10s，这里的 checkpoint 密得多 —— 但进程被 KILL 之后
    # 一次也不会再发生，这正是租约能判定它已死的原因。
    await sleep_with_checkpoint(ctx, 300.0, step=0.05)
    return Done(result="不该走到这里：进程本该在这中间被 kill 掉")


register(KIND, replace=True)(flow)

#: 与生产 profile 同构，只是并发压小、且不挂回收定时器
PROFILE = WorkerProfile(
    name="crash-test",
    lane=LANE_CPU,
    max_jobs=2,
    scaling="测试用，不扩容",
    job_timeout=600.0,
    sweep=False,
)


def __getattr__(name: str) -> Any:
    if name == "WorkerSettings":
        return build_settings(PROFILE)
    raise AttributeError(name)
