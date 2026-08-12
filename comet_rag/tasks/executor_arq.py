"""`TaskExecutor` 的 ARQ 实现：生产端与消费端被 Redis 隔开在两个进程里。

## 队列里只放 task_id

`enqueue_job("run_task", task_id)` —— 参数就这一个，任务数据一律回 `TaskStore` 读。
这不是为了省几个字节，而是**整套语义的地基**：

  * 重试、崩溃恢复、断点续跑退化成同一个动作 `submit(task_id)`；
  * 任务状态只有一份真相（库里那份），队列里不会存在一份过期副本；
  * 消息体不随文档大小膨胀，Redis 不会被当成对象存储用。

反过来说：一旦把 request/context 塞进消息，上面三条会同时失效——而且是
在生产环境里以"重试跑的是旧参数"这种极难复现的形式失效。

## 与 InProcessExecutor 的差别只有三处

    拉起方式    本地 create_task          ↔  Redis 队列 + 独立 worker 进程
    重试排队    detach 一个延迟协程        ↔  enqueue_job(_defer_by=delay)
    取消        协作取消 + 本地 hard cancel ↔  **只有**协作取消

其余全部走 `StoreDrivenExecutor`。这样"换个部署方式行为不变"才是句实话，
而不是两份代码碰巧长得像。

## 并发闸门在 worker，不在这里

`ArqExecutor.submit()` 只是入队，它**不该**限流：生产端限流拦不住别的生产端，
真正的闸门必须在消费端（arq 的 `max_jobs`）。把闸门放错边的后果是模型服务
照样被打爆，而监控上看生产端一切正常。
"""

from __future__ import annotations

import asyncio
from typing import Any

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from .executor import StoreDrivenExecutor, logger
from .models import TaskStatus
from .runner import LANE_CPU, LANE_IO
from .store import TaskStore

#: arq 侧的函数名。worker 注册的 `run_task` 必须与之一致，否则任务入队即失踪。
JOB_NAME = "run_task"

#: 默认队列名。与 arq 的 `arq:queue` 区分开，免得跟同 Redis 里别的 arq 应用串味。
DEFAULT_QUEUE = "comet:queue"

#: 负载道 → 队列名。**一条道一条队列**是分道扩容能成立的前提：
#: 共用队列的话，CPU 密集的活会被 IO worker 捞走，分道就白分了。
LANE_QUEUES: dict[str, str] = {LANE_CPU: "comet:cpu", LANE_IO: "comet:io"}


class ArqExecutor(StoreDrivenExecutor):
    def __init__(
        self,
        store: TaskStore,
        *,
        redis_url: str = "redis://localhost:6379/0",
        queue_name: str = DEFAULT_QUEUE,
        job_name: str = JOB_NAME,
        default_timeout: float | None = None,
        retry_backoff: float = 1.0,
        worker_id: str | None = None,
        pool: ArqRedis | None = None,
        lane: str | None = None,
        lanes: dict[str, str] | None = None,
        entry_lane: str | None = None,
    ) -> None:
        super().__init__(
            store,
            default_timeout=default_timeout,
            retry_backoff=retry_backoff,
            worker_id=worker_id,
            worker_prefix="arq",
            lane=lane,
        )
        self._redis_url = redis_url
        self._queue = queue_name
        self._job_name = job_name
        self._pool = pool
        #: lane → 队列。空 dict = 不分道，一切都进 `queue_name`。
        self._lanes = dict(lanes or {})
        #: 生产端投递的默认道（任务的第一个阶段在哪条道上）。不分道时为 None。
        self._entry_lane = entry_lane
        #: 传进来的池归调用方管，我们不能替它关（worker 与 executor 常共用一个池）
        self._owns_pool = pool is None
        self._pool_lock = asyncio.Lock()

    # ── Redis 连接 ─────────────────────────────────────────────────────────

    async def pool(self) -> ArqRedis:
        """惰性建池，**建一次用到底**。

        这是 spec A3 选 ARQ 而非 Celery 的核心理由之一，必须真的兑现：
        每次 submit 都新建连接的话，高频入库场景下光是 TCP + AUTH 往返
        就能吃掉大部分延迟，连接数还会随并发线性涨到把 Redis 打满。
        """
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:  # 双检：等锁期间可能已被别人建好
                    self._pool = await create_pool(
                        RedisSettings.from_dsn(self._redis_url),
                        default_queue_name=self._queue,
                    )
                    logger.info("ARQ 连接池已建立 queue=%s", self._queue)
        return self._pool

    # ── 提交 ───────────────────────────────────────────────────────────────

    def job_id_for(self, task_id: str, attempts: int, lane: str | None = None) -> str:
        """入队幂等键。

        **不能只用 task_id**（todo.md 原文如此，实测行不通）：arq 的
        `enqueue_job` 在 `arq:job:{id}` 或 `arq:result:{id}` 存在时直接返回
        None，而结果键默认保留一小时。只用 task_id 的话，第一次失败后的重试
        会被自己上一轮的结果键挡掉，任务就永远停在 PENDING 了——而且不报错。

        带上 attempts 后两件事同时成立：同一次尝试内重复投递被去重（幂等），
        跨尝试的重投是新 job（可重试）。

        再带上 lane，是因为**移交不涨 attempts**（移交不是一次尝试）：
        cpu 道跑完投给 io 道时 attempts 没变，只靠 attempts 的话新 job 会被
        上一条的结果键挡掉 —— 又是同一个"静默卡死"。
        """
        return (
            f"{task_id}:{attempts}" if lane is None else f"{task_id}:{attempts}:{lane}"
        )

    def _target_lane(self, lane: str | None) -> str | None:
        """没指名道姓时投哪条道。

        · worker 侧重投（`_schedule_retry`）→ 本 worker 自己那条道，
          因为失败的阶段本来就属于它；
        · 生产端首次投递 → `entry_lane`，即流水线第一个阶段所在的道。

        顺带一提，投错道是**自愈**的：目标 worker 拿到后，流水线发现
        `resume_stage` 所在的道与自己不同，会立刻再移交一次。所以
        `TaskService.retry()` 这种不知道该投哪儿的调用者可以放心用默认值。
        """
        return lane if lane is not None else (self._lane or self._entry_lane)

    def _queue_for(self, lane: str | None) -> str:
        return self._lanes.get(lane, self._queue) if lane else self._queue

    async def submit(
        self, task_id: str, *, delay: float = 0.0, lane: str | None = None
    ) -> None:
        if self._closed:
            raise RuntimeError("执行器已关停，拒绝新任务")
        task = await self._store.require(task_id)
        if task.status is not TaskStatus.PENDING:
            raise ValueError(f"只有 PENDING 可提交，当前 {task.status.value}")

        target = self._target_lane(lane)
        pool = await self.pool()
        job = await pool.enqueue_job(
            self._job_name,
            task_id,  # ← 载荷**只有** task_id
            _job_id=self.job_id_for(task_id, task.attempts, target),
            _queue_name=self._queue_for(target),
            _defer_by=delay or None,
        )
        if job is None:
            # 同一次尝试已在队列里或刚跑完。这是幂等生效，不是错误。
            logger.debug("任务 %s 重复投递已去重", task_id)

    async def _schedule_retry(self, task_id: str, delay: float) -> None:
        """退避重投。延迟交给 Redis 的有序集合，不占 worker 的执行槽位。

        进程内实现必须 detach 一个协程来等（否则本轮协程收不了尾），
        跨进程这里反而更简单：入队即返回，谁先空出来谁来跑。
        """
        await self.submit(task_id, delay=delay)

    async def _schedule_handoff(self, task_id: str, lane: str) -> None:
        """把任务投到目标道的队列上。这就是分道扩容的全部机制。"""
        await self.submit(task_id, lane=lane)

    # ── 取消 ───────────────────────────────────────────────────────────────
    #
    # 直接用基类的 `_accept_cancel`：跨进程时**只有**协作取消这一条路。
    # `asyncio.Task.cancel()` 传不过进程边界，arq 的 `abort_job` 又需要
    # `allow_abort_jobs` 且是硬中断——会让 runner 死在半路，连状态都来不及写。
    # 我们要的是 runner 在 `ctx.checkpoint()` 里看见 CANCELLING 后干净退出。

    # ── 关停 ───────────────────────────────────────────────────────────────

    async def shutdown(self, *, timeout: float | None = 10.0) -> None:
        """停止收新任务，排干**本实例**在跑的任务，然后放掉连接池。

        "本实例"是关键限定：生产端进程关停时不该、也没能力去停别的 worker 上
        在跑的任务。worker 进程侧由 arq 的 `on_shutdown` 调到这里，届时
        `_inflight` 才是非空的。
        """
        self._closed = True
        for task_id in list(self._inflight):
            await self.request_cancel(task_id)
        try:
            await asyncio.wait_for(self._drain(), timeout)
        except TimeoutError:
            # 剩下的交给 sweep_stale（T24）按租约回收——比在这里强行改状态安全：
            # 那些 runner 可能还活着，两边同时写会造成"一份任务两个执行者"。
            logger.warning(
                "关停超时，%d 个任务仍在跑，留给租约回收", len(self._inflight)
            )
        if self._owns_pool and self._pool is not None:
            await self._pool.aclose()
            self._pool = None

    async def _drain(self) -> None:
        while self._inflight:
            await asyncio.sleep(0.01)
        await self.wait_all()


# ── worker 侧入口 ──────────────────────────────────────────────────────────


async def run_task(ctx: dict[str, Any], task_id: str) -> None:
    """arq 的 job 函数。**签名里只有 task_id**，这是本模块全部承诺的落点。

    `ctx["executor"]` 由 worker 的 `on_startup` 装配（见 `workers/`，T23）。
    这里刻意不吞异常：`execute()` 已经把 runner 的所有异常收口成任务状态了，
    还能漏到这一层的只有基础设施故障（库连不上之类），那种情况该让 arq
    按自己的 `max_tries` 重投，而不是被我们静默咽掉。
    """
    executor: ArqExecutor = ctx["executor"]
    await executor.execute(task_id)


__all__ = ["DEFAULT_QUEUE", "JOB_NAME", "ArqExecutor", "run_task"]
