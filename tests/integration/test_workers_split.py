"""分道 worker：一条流水线跨两个 worker 进程跑完（T23）。

验的是 plan Checkpoint E 的第二条：**preprocessor 与 embedder 各干各的那段**。
分道要是只停留在文档里，症状是"看起来都在跑、其实两个 worker 干着同样的活"，
从外部完全看不出来 —— 所以这里逐阶段断言是谁执行的。

worker 跑在当前事件循环里（arq 的 `async_run()`），调度全走真实 Redis：
两条队列、移交、去重、`max_jobs` 闸门都是真的，少掉的只有 fork。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from arq.worker import Worker

from comet_rag.tasks import (
    LANE_CPU,
    LANE_IO,
    Done,
    RetriableError,
    StagePipeline,
    TaskContext,
    TaskStatus,
    register,
)
from comet_rag.tasks.executor_arq import ArqExecutor, run_task
from comet_rag.tasks.store import InMemoryTaskStore, TaskStore
from tests.contracts.support import wait_for_terminal, wait_until

pytestmark = pytest.mark.integration


#: 每个用例一对独立队列，免得上一个用例的遗留 job 被下一个消费掉
def _queues() -> dict[str, str]:
    tag = uuid4().hex[:8]
    return {LANE_CPU: f"split:{tag}:cpu", LANE_IO: f"split:{tag}:io"}


# ── 一条分道流水线 ─────────────────────────────────────────────────────────

flow = StagePipeline()


@flow.stage("parsing", lane=LANE_CPU)
async def _parsing(ctx: TaskContext) -> None:
    await ctx.put(trace=[*(await _trace(ctx)), f"parsing@{ctx.lane}"])


@flow.stage("splitting", lane=LANE_CPU)
async def _splitting(ctx: TaskContext) -> None:
    await ctx.put(trace=[*(await _trace(ctx)), f"splitting@{ctx.lane}"])


@flow.stage("indexing", lane=LANE_IO)
async def _indexing(ctx: TaskContext) -> Done:
    trace = [*(await _trace(ctx)), f"indexing@{ctx.lane}"]
    await ctx.put(trace=trace)
    return Done(result={"trace": trace, "worker": ctx.worker_id})


register("split-flow")(flow)


#: 只在 io 道上失败一次，用来验证"移交没白吃掉重试预算"
_FLAKY_HITS: dict[str, int] = {}

flaky_flow = StagePipeline()


@flaky_flow.stage("parsing", lane=LANE_CPU)
async def _f_parsing(ctx: TaskContext) -> None:
    await ctx.put(parsed=True)


@flaky_flow.stage("indexing", lane=LANE_IO)
async def _f_indexing(ctx: TaskContext) -> Done:
    _FLAKY_HITS[ctx.task_id] = _FLAKY_HITS.get(ctx.task_id, 0) + 1
    if _FLAKY_HITS[ctx.task_id] < 2:
        raise RetriableError("io 道第一次失败", code="split_503")
    return Done(result={"io_tries": _FLAKY_HITS[ctx.task_id]})


register("split-flaky")(flaky_flow)


async def _trace(ctx: TaskContext) -> list[str]:
    return (await ctx.snapshot()).context.get("trace", [])


# ── 夹具 ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def pool(redis_url: str) -> AsyncIterator[ArqRedis]:
    p = await create_pool(RedisSettings.from_dsn(redis_url))
    try:
        yield p
    finally:
        await p.aclose()


def _worker(pool: ArqRedis, queue: str, executor: ArqExecutor, max_jobs: int) -> Worker:
    return Worker(
        functions=[run_task],
        redis_pool=pool,
        queue_name=queue,
        max_jobs=max_jobs,
        poll_delay=0.02,
        handle_signals=False,
        ctx={"executor": executor},
        retry_jobs=False,
        max_tries=1,
        keep_result=2,
        log_results=False,
    )


@contextlib.asynccontextmanager
async def _running(*workers: Worker) -> AsyncIterator[None]:
    runners = [asyncio.create_task(w.async_run()) for w in workers]
    try:
        yield
    finally:
        for r in runners:
            r.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        for w in workers:
            for job in list(w.tasks.values()):
                job.cancel()
            if w.tasks:
                await asyncio.gather(*w.tasks.values(), return_exceptions=True)


class _Lanes:
    """一套完整的分道部署：一个生产端 + 两个分道 worker。"""

    def __init__(self, store: TaskStore, pool: ArqRedis) -> None:
        self.queues = _queues()
        common = {
            "lanes": self.queues,
            "entry_lane": LANE_CPU,
            "retry_backoff": 0.01,
            "pool": pool,
        }
        #: API 进程里那个：只投递，不执行，故 lane=None
        self.producer = ArqExecutor(store, **common)
        self.cpu = ArqExecutor(store, lane=LANE_CPU, **common)
        self.io = ArqExecutor(store, lane=LANE_IO, **common)
        self.cpu_worker = _worker(pool, self.queues[LANE_CPU], self.cpu, 2)
        self.io_worker = _worker(pool, self.queues[LANE_IO], self.io, 8)


@pytest.fixture
def lanes(pool: ArqRedis) -> _Lanes:
    return _Lanes(InMemoryTaskStore(), pool)


@pytest.fixture
def store(lanes: _Lanes) -> TaskStore:
    return lanes.producer._store  # noqa: SLF001


# ── 用例 ───────────────────────────────────────────────────────────────────


async def test_pipeline_crosses_from_cpu_lane_to_io_lane(
    lanes: _Lanes, store: TaskStore
) -> None:
    """三个阶段跑在两个 worker 上，且**每个阶段落在它声明的那条道**。"""
    async with _running(lanes.cpu_worker, lanes.io_worker):
        task = await store.create("split-flow")
        await lanes.producer.submit(task.task_id)
        done = await wait_for_terminal(store, task.task_id, timeout=20.0)

    assert done.status is TaskStatus.SUCCEEDED
    assert done.result["trace"] == [
        f"parsing@{LANE_CPU}",
        f"splitting@{LANE_CPU}",
        f"indexing@{LANE_IO}",
    ]
    # 最后一段是 io worker 干的 —— 全在一个 worker 里跑完的话这里会是 cpu 那个
    assert done.result["worker"] == lanes.io.worker_id
    assert [s.stage for s in done.stage_history] == [
        "parsing",
        "splitting",
        "indexing",
    ]


async def test_task_stalls_when_the_other_lane_has_no_worker(
    lanes: _Lanes, store: TaskStore
) -> None:
    """只起 preprocessor，任务会**停在 io 队列上不动**。

    这条同时是分道的反向验证：分道要是没生效（两个 worker 干一样的活），
    单起一个照样能跑完，本用例就会红。
    它也记录了一个真实的排查陷阱 —— 任务卡住时状态是 PENDING、无错误、
    无日志，从 API 上完全看不出少起了一个 worker。
    """
    async with _running(lanes.cpu_worker):  # 故意不起 io worker
        task = await store.create("split-flow")
        await lanes.producer.submit(task.task_id)

        await wait_until(
            lambda: _resumed_at(store, task.task_id, "indexing"),
            timeout=20.0,
            message="cpu 道没把任务移交出去",
        )
        await asyncio.sleep(0.5)
        stalled = await store.require(task.task_id)

    assert stalled.status is TaskStatus.PENDING, "没有 io worker 却把任务跑完了"
    assert stalled.resume_stage == "indexing"
    assert stalled.error is None, "移交不是失败，不该留下错误"

    # 补上 io worker，任务立刻续跑完成 —— 移交出去的东西是完好的
    async with _running(lanes.io_worker):
        done = await wait_for_terminal(store, task.task_id, timeout=20.0)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.result["trace"][-1] == f"indexing@{LANE_IO}"


async def test_handoff_does_not_consume_a_retry_attempt(
    lanes: _Lanes, store: TaskStore
) -> None:
    """移交是正常流程，不是一次失败的尝试。

    不退这笔账的话，一条两次移交的流水线开跑即吃掉 2 次重试预算 ——
    等真出故障时反而没得重试了。本用例给 max_attempts=2、并让 io 道失败
    一次：只有"移交不计数"成立，那唯一的一次重试才够用。
    """
    _FLAKY_HITS.clear()
    async with _running(lanes.cpu_worker, lanes.io_worker):
        task = await store.create("split-flaky", max_attempts=2)
        await lanes.producer.submit(task.task_id)
        done = await wait_for_terminal(store, task.task_id, timeout=20.0)

    assert done.status is TaskStatus.SUCCEEDED, (
        f"重试预算被移交吃掉了：attempts={done.attempts} error={done.error}"
    )
    assert done.result == {"io_tries": 2}
    # 两次真正的尝试：io 道失败那次 + 成功那次。cpu 道那次已被移交退回。
    assert done.attempts == 2


async def test_handoff_is_routed_to_the_other_queue_not_its_own(
    lanes: _Lanes, store: TaskStore, pool: ArqRedis
) -> None:
    """移交必须进**另一条**队列。投回自己那条的话，cpu worker 会反复捞起
    同一个任务、发现还是该移交、再投回来 —— 一个不报错的忙等死循环。"""
    async with _running(lanes.cpu_worker):
        task = await store.create("split-flow")
        await lanes.producer.submit(task.task_id)
        await wait_until(
            lambda: _resumed_at(store, task.task_id, "indexing"),
            timeout=20.0,
            message="cpu 道没把任务移交出去",
        )
        await asyncio.sleep(0.3)  # 给"投错队列"足够时间暴露

        cpu_depth = await pool.zcard(lanes.queues[LANE_CPU])
        io_depth = await pool.zcard(lanes.queues[LANE_IO])

    assert io_depth == 1, f"io 队列里应有 1 个待办，实际 {io_depth}"
    assert cpu_depth == 0, f"任务被投回了自己那条队列（深度 {cpu_depth}）"


async def _resumed_at(store: TaskStore, task_id: str, stage: str) -> bool:
    task = await store.get(task_id)
    return (
        task is not None
        and task.status is TaskStatus.PENDING
        and task.resume_stage == stage
    )
