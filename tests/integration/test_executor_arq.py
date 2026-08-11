"""`ArqExecutor` 跑与 `InProcessExecutor` **同一套**执行器契约（T22）。

契约当初就是照着这一天设计的：**只通过 TaskStore 观察结果，绝不 gather
执行器的内部协程**。所以本文件一条契约断言都没改——如果需要改，那说明
`TaskExecutor` 这个抽象根本没兜住跨进程，该动的是抽象。

## 测试里的 worker 为什么跑在同一个进程

arq 的 `Worker` 可以直接在当前事件循环里跑（`async_run()`）。这样做**不会**
削弱验证强度：调度依然整个走真实 Redis（入队、去重、延迟投递、`max_jobs`
闸门全是真的），少掉的只有 `fork` 本身。

真正"跨进程"的那部分——生产端与消费端之间除了 Redis 与 TaskStore 再无别的
通道——由下半部分的专项用例证明：那里用**两个 ArqExecutor 实例 + 两个独立
的 Postgres 连接**，任何进程内的暗道都会让它们失败。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from arq.jobs import Job
from arq.worker import Worker
from sqlalchemy import text

from comet_rag.infrastructure.database import Database
from comet_rag.tasks import (
    Done,
    InMemoryTaskStore,
    RetriableError,
    TaskContext,
    TaskStatus,
    TaskStore,
    register,
    sleep_with_checkpoint,
)
from comet_rag.tasks.executor_arq import JOB_NAME, ArqExecutor, run_task
from comet_rag.tasks.store_postgres import PostgresTaskStore
from tests.contracts.support import wait_for_terminal, wait_until
from tests.contracts.task_executor import TaskExecutorContract

pytestmark = pytest.mark.integration


#: 每个用例一条独立队列。共用队列时，上一个用例遗留的 job 会在下一个用例里
#: 被消费，症状是"偶发的、与本用例无关的失败"——那类问题排查成本极高。
def _fresh_queue() -> str:
    return f"comet:test:{uuid4().hex[:8]}"


@pytest.fixture
async def pool(redis_url: str) -> AsyncIterator[ArqRedis]:
    p = await create_pool(RedisSettings.from_dsn(redis_url))
    try:
        yield p
    finally:
        await p.aclose()


def _make_worker(
    pool: ArqRedis, queue: str, executor: ArqExecutor, *, max_jobs: int
) -> Worker:
    return Worker(
        functions=[run_task],
        redis_pool=pool,
        queue_name=queue,
        max_jobs=max_jobs,
        # 默认 0.5s 会让每个用例都白等半秒；语义不受影响，只是轮询更勤
        poll_delay=0.02,
        handle_signals=False,  # 测试进程里抢信号会把 pytest 一起带走
        ctx={"executor": executor},
        # 重试归 TaskStore 管（attempts / max_attempts / 退避）。让 arq 也插一脚
        # 会出现"库里记 2 次、实际跑了 6 次"，两套计数谁也不对。
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
        # main 被取消不会带走已经起飞的 job 协程，得单独收
        for job in list(worker.tasks.values()):
            job.cancel()
        if worker.tasks:
            await asyncio.gather(*worker.tasks.values(), return_exceptions=True)


# ── 契约 ───────────────────────────────────────────────────────────────────


class TestArqExecutor(TaskExecutorContract):
    #: 跨进程要多给点时间：入队、轮询、Redis 往返都是真的
    timeout = 15.0
    #: arq 的闸门在 worker 的 max_jobs 上，不在 submit 侧
    max_concurrency = 4

    @pytest.fixture
    async def store(self) -> TaskStore:
        # 用内存 store 是有意的：本类要验的是**执行器**，把 Postgres 也拉进来
        # 只会让失败原因变模糊。跨存储的部分由下面的专项用例覆盖。
        return InMemoryTaskStore()

    @pytest.fixture
    async def executor(
        self, store: TaskStore, pool: ArqRedis
    ) -> AsyncIterator[ArqExecutor]:
        queue = _fresh_queue()
        # retry_backoff 压到 10ms：退避语义由断言覆盖，不靠真等
        ex = ArqExecutor(store, queue_name=queue, retry_backoff=0.01, pool=pool)
        worker = _make_worker(pool, queue, ex, max_jobs=self.max_concurrency)
        async with _running(worker):
            yield ex
            await ex.shutdown(timeout=self.timeout)


# ── ARQ 特有：契约兜不住的跨进程性质 ────────────────────────────────────────


@register("arq-echo")
async def _echo(ctx: TaskContext) -> Done:
    task = await ctx.snapshot()
    return Done(result={"kb": task.request, "worker": ctx.worker_id})


@register("arq-slow")
async def _slow(ctx: TaskContext) -> Done:
    await ctx.enter_stage("working")
    await sleep_with_checkpoint(ctx, 10.0, step=0.02)
    return Done(result="不该走到这里")


@register("arq-flaky")
async def _flaky(ctx: TaskContext) -> Done:
    task = await ctx.snapshot()
    if task.attempts < 2:
        raise RetriableError("第一次总是失败", code="arq_503")
    return Done(result={"attempts": task.attempts})


async def test_enqueued_payload_carries_only_the_task_id(
    pool: ArqRedis, redis_url: str
) -> None:
    """**队列里只放 task_id** —— 这是重试/恢复/续跑能统一成一个动作的地基。

    有效载荷一旦带上 request/context，重试跑的就是入队那一刻的旧参数，
    而任务状态在库里早就变了。那种 bug 只在重试路径上出现，几乎不可能
    在开发机上撞见。所以这里直接把 Redis 里的消息体挖出来验。
    """
    store = InMemoryTaskStore()
    queue = _fresh_queue()
    executor = ArqExecutor(store, queue_name=queue, pool=pool)
    task = await store.create("arq-echo", request={"kb_id": "kb-1", "source": "a.docx"})

    # 故意不起 worker：让消息留在队列里供检查
    await executor.submit(task.task_id)

    job = Job(executor.job_id_for(task.task_id, 0), redis=pool, _queue_name=queue)
    info = await job.info()
    assert info is not None, "任务没进队列"
    assert info.function == JOB_NAME
    assert info.args == (task.task_id,), f"载荷里混进了任务数据：{info.args}"
    assert info.kwargs == {}
    # request 里的东西一个字都不该出现在消息体里
    assert "a.docx" not in repr(info.args) + repr(info.kwargs)


async def test_redis_pool_is_created_once_and_reused(redis_url: str) -> None:
    """spec A3 选 ARQ 而非 Celery 的核心理由之一，必须真的兑现。

    每次 submit 新建连接的话，光 TCP + AUTH 往返就能吃掉大部分入队延迟，
    连接数还会随并发线性涨到把 Redis 打满。
    """
    store = InMemoryTaskStore()
    executor = ArqExecutor(store, redis_url=redis_url, queue_name=_fresh_queue())
    try:
        # 并发首调：双检锁要是写漏了，这里会建出两个池
        pools = await asyncio.gather(*(executor.pool() for _ in range(10)))
        assert len({id(p) for p in pools}) == 1, "并发首调建出了多个连接池"

        first = pools[0]
        conn_pool = first.connection_pool
        for _ in range(20):
            task = await store.create("arq-echo")
            await executor.submit(task.task_id)

        assert await executor.pool() is first, "submit 期间连接池被换掉了"
        assert (await executor.pool()).connection_pool is conn_pool
    finally:
        await executor.shutdown(timeout=5.0)


async def test_job_id_must_vary_per_attempt(pool: ArqRedis) -> None:
    """反向验证：把入队幂等键退化成"只用 task_id"，重试会**永远卡住**。

    todo.md 原本写的就是"`_job_id` 用 `task_id` 保证幂等"。实测行不通：
    arq 的 `enqueue_job` 在 `arq:result:{id}` 存在时直接返回 None，而结果键
    默认留一小时——第一次失败后的重投会被自己上一轮的结果键挡掉，任务
    停在 PENDING 且**不报任何错**。这条用例就是那个 bug 的现场。
    """

    class _NaiveJobId(ArqExecutor):
        def job_id_for(self, task_id: str, attempts: int) -> str:
            return task_id  # ← todo.md 的原始写法

        # 让 arq 的结果键更快过期都救不了：默认一小时，实际部署里必挂

    store = InMemoryTaskStore()
    queue = _fresh_queue()
    broken = _NaiveJobId(store, queue_name=queue, retry_backoff=0.01, pool=pool)
    worker = _make_worker(pool, queue, broken, max_jobs=2)

    async with _running(worker):
        task = await store.create("arq-flaky", max_attempts=3)
        await broken.submit(task.task_id)

        # 第一次尝试必然失败并退回 PENDING
        await wait_until(
            lambda: _attempts_reached(store, task.task_id, 1),
            timeout=10.0,
            message="第一次尝试没跑起来",
        )
        # 然后就再也不动了 —— 重投被自己的结果键挡掉
        await asyncio.sleep(1.0)
        stuck = await store.require(task.task_id)

    assert stuck.status is TaskStatus.PENDING, (
        f"退化版竟然推进到了 {stuck.status.value} —— "
        "说明这条反向验证已经测不到东西了，需要重新设计"
    )
    assert stuck.attempts == 1

    # 对照：正确的 job_id 让同一个任务顺利重试成功
    queue2 = _fresh_queue()
    good = ArqExecutor(store, queue_name=queue2, retry_backoff=0.01, pool=pool)
    worker2 = _make_worker(pool, queue2, good, max_jobs=2)
    async with _running(worker2):
        await good.submit(stuck.task_id)
        done = await wait_for_terminal(store, stuck.task_id, timeout=15.0)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.attempts == 2


async def _attempts_reached(store: TaskStore, task_id: str, n: int) -> bool:
    task = await store.get(task_id)
    return task is not None and task.attempts >= n


# ── 真·跨边界：两个执行器 + 两条独立数据库连接 ─────────────────────────────


async def _clean(dsn: str) -> None:
    db = Database(dsn)
    try:
        async with db.session() as session:
            exists = (
                await session.execute(text("SELECT to_regclass('tasks')"))
            ).scalar_one()
            if exists is None:
                pytest.skip("tasks 表不存在，先跑 `uv run alembic upgrade head`")
        async with db.transaction() as session:
            await session.execute(text("TRUNCATE TABLE tasks CASCADE"))
    finally:
        await db.aclose()


@pytest.fixture
async def split_pair(
    postgres_dsn: str, pool: ArqRedis
) -> AsyncIterator[tuple[ArqExecutor, ArqExecutor, TaskStore, Any]]:
    """生产端与消费端**各自**持有连接，之间只剩 Redis 与 Postgres 两条通道。

    两个 `Database` 实例意味着两个连接池、两套 SQLAlchemy 身份映射：任何
    "其实是同一个对象所以看得见"的假通过，都会在这个布置下露馅。
    """
    await _clean(postgres_dsn)
    db_producer, db_consumer = Database(postgres_dsn), Database(postgres_dsn)
    store_producer = PostgresTaskStore(db_producer)
    store_consumer = PostgresTaskStore(db_consumer)

    queue = _fresh_queue()
    producer = ArqExecutor(
        store_producer, queue_name=queue, retry_backoff=0.01, pool=pool
    )
    consumer = ArqExecutor(
        store_consumer, queue_name=queue, retry_backoff=0.01, pool=pool
    )
    worker = _make_worker(pool, queue, consumer, max_jobs=4)

    try:
        async with _running(worker):
            yield producer, consumer, store_producer, store_consumer
            await producer.shutdown(timeout=10.0)
            await consumer.shutdown(timeout=10.0)
    finally:
        await db_producer.aclose()
        await db_consumer.aclose()
        await _clean(postgres_dsn)


async def test_task_crosses_the_boundary_with_only_redis_and_postgres(
    split_pair: tuple[ArqExecutor, ArqExecutor, TaskStore, TaskStore],
) -> None:
    producer, consumer, store_p, store_c = split_pair

    task = await store_p.create("arq-echo", request={"kb_id": "kb-cross"})
    await producer.submit(task.task_id)

    done = await wait_for_terminal(store_p, task.task_id, timeout=15.0)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.result["kb"] == {"kb_id": "kb-cross"}
    # 干活的是消费端那个执行器，生产端全程没碰过 runner
    assert done.result["worker"] == consumer.worker_id
    assert done.worker_id == consumer.worker_id


async def test_cancel_written_by_producer_is_honoured_by_consumer(
    split_pair: tuple[ArqExecutor, ArqExecutor, TaskStore, TaskStore],
) -> None:
    """跨进程取消的**唯一**通道是 TaskStore 里的 CANCELLING 标记。

    `asyncio.Task.cancel()` 传不过进程边界，所以取消必然是协作式的：
    生产端只负责写状态，真正停下来靠 runner 走到 `ctx.checkpoint()`。
    """
    producer, _consumer, store_p, _store_c = split_pair

    task = await store_p.create("arq-slow")
    await producer.submit(task.task_id)
    await wait_until(
        lambda: _is_running(store_p, task.task_id),
        timeout=15.0,
        message="任务未在消费端跑起来",
    )

    assert await producer.request_cancel(task.task_id) is True

    done = await wait_for_terminal(store_p, task.task_id, timeout=15.0)
    assert done.status is TaskStatus.CANCELLED
    assert done.finished_at is not None


async def test_retry_is_rescheduled_through_the_queue(
    split_pair: tuple[ArqExecutor, ArqExecutor, TaskStore, TaskStore],
) -> None:
    """重试也走队列：谁先空出来谁接手，而不是绑死在上一轮那个 worker 上。

    注意重投是**消费端**发起的（`_mark_failed` 在 worker 里跑），生产端此刻
    早就返回了。这正是"重试 = 再入队一次 task_id"的实际形态。
    """
    producer, _consumer, store_p, _store_c = split_pair

    task = await store_p.create("arq-flaky", max_attempts=3)
    await producer.submit(task.task_id)

    done = await wait_for_terminal(store_p, task.task_id, timeout=20.0)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.attempts == 2
    assert done.result == {"attempts": 2}


async def _is_running(store: TaskStore, task_id: str) -> bool:
    task = await store.get(task_id)
    return task is not None and task.status is TaskStatus.RUNNING
