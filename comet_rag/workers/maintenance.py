"""回收失去租约的跨进程任务并重新入队。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from arq import cron
from arq.cron import CronJob

from comet_rag.core.logging import logger
from comet_rag.tasks import TaskExecutor, TaskStore

#: 回收租约。远大于 `TaskContext.heartbeat_interval`（10s）—— 见模块文档。
DEFAULT_LEASE = timedelta(seconds=90)

#: 每分钟扫一次。再密没意义：lease 是 90 秒，扫得再勤也不会更早发现。
SWEEP_SECOND = 7


async def sweep_stale_tasks(ctx: dict[str, Any]) -> int:
    """回收心跳超时的 RUNNING 任务，并把它们**重新投回队列**。

    第二步容易被漏掉，漏了整个机制就是空的：`sweep_stale` 只改数据库状态，
    而 ARQ 部署下"PENDING"不代表队列里有它 —— 任务会从"卡在 RUNNING"
    变成"卡在 PENDING"，看着更健康，实际一样没人跑。
    """
    store: TaskStore = ctx["context"].task_store
    executor: TaskExecutor = ctx["executor"]
    lease: timedelta = ctx.get("lease", DEFAULT_LEASE)

    revived = await store.sweep_stale(lease)
    if not revived:
        return 0

    logger.warning(
        f"租约回收 {len(revived)} 个任务（lease={lease.total_seconds():.0f}s）："
        f"{', '.join(revived[:10])}{' …' if len(revived) > 10 else ''}"
    )
    requeued = 0
    for task_id in revived:
        try:
            # 已判死（重试次数耗尽）的那些不是 PENDING，submit 会拒绝 —— 正常，
            # 它们该以 FAILED 结束，不该再被投出去。
            await executor.submit(task_id)
            requeued += 1
        except ValueError:
            logger.info(f"任务 {task_id} 回收后已判失败，不再重投")
        except Exception:
            # 一个任务重投失败不该让整轮回收停摆，下一轮还会再试
            logger.exception(f"任务 {task_id} 回收后重投失败")
    return requeued


def sweep_cron(*, second: int = SWEEP_SECOND) -> CronJob:
    """产出 arq 的 cron 定义。

    `unique=True`（arq 默认）让多副本下每个时刻只有一个 worker 真的执行；
    `run_at_startup=True` 则让**整个集群刚起来时**立刻扫一遍 —— 上一轮部署
    留下的僵尸任务不用再等一分钟。
    """
    return cron(
        sweep_stale_tasks,
        name="sweep_stale_tasks",
        second=second,
        run_at_startup=True,
        unique=True,
        # 回收本身很快；给足超时只是防它卡在数据库上把 cron 槽位占死
        timeout=120,
        max_tries=1,
    )


__all__ = ["DEFAULT_LEASE", "SWEEP_SECOND", "sweep_cron", "sweep_stale_tasks"]
