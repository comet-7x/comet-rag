"""租约回收：把崩掉的 worker 留下的僵尸任务捞回来（T24）。

## 为什么跨进程**必须**有它

单进程模式下 worker 死了，任务也随进程消失，没人会看到半截状态。跨进程不同：
worker 被 `kill -9` 时，PostgreSQL 里那条任务**永远停在 RUNNING**。没有人再
推进它，也没有人报错 —— 它只是安静地卡在那儿，直到有人去查为什么某份文档
一天了还没入库。

回收的判据是**心跳**：runner 每次 `ctx.checkpoint()` 会续租，超过 `lease`
没续上就判定 worker 已死，把任务退回 PENDING（还有重试次数）或判失败。

## lease 必须远大于心跳间隔

心跳每 10 秒一次（`TaskContext.heartbeat_interval`），lease 取 **90 秒**——
9 倍余量。取得太紧的后果是**误判**：worker 活得好好的，只是某一步慢了、
或者 GC 卡了一下，任务就被抢走，于是同一份活有两个人在做。

误判无法根除（"没心跳"和"死了"本来就分不清），所以执行器侧还有一道围栏：
`StoreDrivenExecutor._still_mine` 会在写终态前确认任务还归自己管，
被抢走了就一个字都不写。**lease 负责少误判，围栏负责误判了也不出错**，
两者缺一不可。

## 定时器挂在哪儿

挂在**每个** worker 上，不单起一个进程。arq 的 cron 默认 `unique=True`：
job id 里带着计划时刻，多个 worker 同时到点也只有一个能真正入队，其余
被去重挡掉。所以多副本是安全的，也不必为一个每分钟跑一次的活单开一个进程。

**单进程模式（`InProcessExecutor`）绝不能挂**：那时协程还活在本进程里，
心跳只是没来得及写，回收会造出"一份任务两个执行者"。所以这个 cron 只在
`workers/` 下注册 —— API 进程与单进程部署根本不会加载它。
"""

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
