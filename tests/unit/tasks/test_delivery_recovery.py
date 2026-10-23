from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from arq import ArqRedis
from arq.constants import result_key_prefix

from comet_rag.core.time import Time
from comet_rag.infrastructure.task_execution.arq import ArqExecutor, run_task
from comet_rag.tasks import (
    LANE_CPU,
    LANE_IO,
    Done,
    InMemoryTaskStore,
    RetriableError,
    StagePipeline,
    TaskContext,
    TaskError,
    TaskService,
    TaskStatus,
)
from comet_rag.workers.maintenance import sweep_stale_tasks


class FakeQueue:
    """只模拟 ARQ 的 job/result 去重与队列元数据，不打开 Redis 连接。"""

    def __init__(self) -> None:
        self.jobs: dict[str, tuple[tuple[str, ...], str, datetime | None]] = {}
        self.results: set[str] = set()
        self.unavailable = False
        self.unavailable_ids: set[str] = set()

    async def enqueue_job(
        self,
        name: str,
        *args: str,
        **options: Any,  # Any：ARQ 异构关键字元数据
    ) -> object | None:
        if self.unavailable or options["_job_id"] in self.unavailable_ids:
            raise ConnectionError("Redis unavailable")
        job_id = options["_job_id"]
        if job_id in self.jobs or result_key_prefix + job_id in self.results:
            return None
        self.jobs[job_id] = (args, options["_queue_name"], options["_defer_until"])
        return object()

    async def exists(self, key: str) -> int:
        return int(key in self.results)

    async def delete(self, key: str) -> int:
        existed = key in self.results
        self.results.discard(key)
        return int(existed)


def executor_for(
    store: InMemoryTaskStore, queue: FakeQueue, *, lane: str | None = None
) -> ArqExecutor:
    return ArqExecutor(
        store,
        pool=cast(ArqRedis, queue),
        retry_backoff=10,
        lane=lane,
        entry_lane=LANE_CPU,
        lanes={LANE_CPU: "cpu-queue", LANE_IO: "io-queue"},
    )


async def sweep(store: InMemoryTaskStore, executor: ArqExecutor) -> int:
    return await sweep_stale_tasks(
        {"context": SimpleNamespace(task_store=store), "executor": executor}
    )


@pytest.mark.parametrize("api_exits_before_enqueue", [False, True])
async def test_initial_delivery_is_repaired_without_stale_running_tasks(
    api_exits_before_enqueue: bool,
) -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue, lane=LANE_IO)
    queue.unavailable = True
    if api_exits_before_enqueue:
        task = await store.create("delivery-echo")
    else:
        with pytest.raises(ConnectionError):
            await TaskService(store, executor).submit("delivery-echo")
        [task] = await store.list_tasks()
    assert task.status is TaskStatus.PENDING
    queue.unavailable = False
    assert await sweep(store, executor) == 1
    [payload] = queue.jobs.values()
    assert payload == ((task.task_id,), "cpu-queue", None)
    assert (await store.require(task.task_id)).attempts == 0
    # 刚启动的另一副本再次对账，不增加消息或执行尝试。
    assert await sweep(store, executor_for(store, queue)) == 1
    assert len(queue.jobs) == 1


async def test_retry_enqueue_failure_preserves_absolute_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue, lane=LANE_CPU)
    now = Time.now()
    monkeypatch.setattr(Time, "now", lambda: now)

    @executor.runners.register("delivery-retry")
    async def runner(ctx: TaskContext) -> Done:
        await ctx.enter_stage("working")
        raise RetriableError("upstream unavailable", code="upstream_503")

    try:
        task = await store.create("delivery-retry", max_attempts=3)
        await executor.submit(task.task_id)
        old_job = next(iter(queue.jobs))
        queue.unavailable = True
        with pytest.raises(ConnectionError):
            await run_task({"executor": executor, "job_id": old_job}, task.task_id)
        pending = await store.require(task.task_id)
        assert pending.status is TaskStatus.PENDING
        assert pending.attempts == 1
        assert pending.resume_stage == "working"
        now += timedelta(seconds=4)
        queue.unavailable = False
        assert await sweep(store, executor) == 1
        job_id = executor.job_id_for(task.task_id, 1, LANE_CPU, version=pending.version)
        assert queue.jobs[job_id][2] == now + timedelta(seconds=6)
        # 重复补投不能改写已有消息的执行时间，也不能再涨 attempts。
        now += timedelta(seconds=2)
        await sweep(store, executor)
        assert queue.jobs[job_id][2] == now + timedelta(seconds=4)
        assert (await store.require(task.task_id)).version == pending.version
    finally:
        await executor.shutdown()
        executor.runners.unregister("delivery-retry")


async def test_first_retry_and_reconciliation_use_the_same_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    now = Time.now()
    monkeypatch.setattr(Time, "now", lambda: now)
    task = await store.create(
        "delivery-retry",
        attempts=2,
        error=TaskError(code="upstream_503", message="retry", retriable=True),
    )
    now += timedelta(seconds=3)
    await executor._schedule_retry(task.task_id, 20)
    [first] = queue.jobs.values()
    assert first[2] == now + timedelta(seconds=17)
    # Redis 丢失全部消息后仍按相同的持久化时间补投，不重算 20s 退避。
    queue.jobs.clear()
    now += timedelta(seconds=5)
    await sweep(store, executor)
    [repaired] = queue.jobs.values()
    assert repaired == first


async def test_handoff_enqueue_failure_is_repaired_to_the_destination_lane() -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    cpu = executor_for(store, queue, lane=LANE_CPU)
    flow = StagePipeline()

    @flow.stage("prepare", lane=LANE_CPU)
    async def prepare(ctx: TaskContext) -> None:
        await ctx.put(prepared=True)

    @flow.stage("index", lane=LANE_IO)
    async def index(ctx: TaskContext) -> Done:
        assert (await ctx.snapshot()).context["prepared"] is True
        return Done(result="indexed")

    cpu.runners.register("delivery-handoff")(flow)
    try:
        task = await store.create("delivery-handoff", max_attempts=1)
        await cpu.submit(task.task_id)
        old_job = next(iter(queue.jobs))
        queue.unavailable = True
        await run_task({"executor": cpu, "job_id": old_job}, task.task_id)
        pending = await store.require(task.task_id)
        assert pending.status is TaskStatus.PENDING
        assert pending.attempts == 0
        assert pending.resume_stage == "index"
        queue.unavailable = False
        await sweep(store, cpu)
        job_id = cpu.job_id_for(task.task_id, 0, LANE_IO, version=pending.version)
        assert queue.jobs[job_id] == ((task.task_id,), "io-queue", None)
        io = executor_for(store, queue, lane=LANE_IO)
        io.runners.register("delivery-handoff")(flow)
        await run_task({"executor": io, "job_id": job_id}, task.task_id)
        done = await store.require(task.task_id)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.result == "indexed"
        assert done.attempts == 1
    finally:
        await cpu.shutdown()
        cpu.runners.unregister("delivery-handoff")


async def test_failed_requeue_after_lease_reclaim_is_repaired_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    now = Time.now()
    monkeypatch.setattr(Time, "now", lambda: now)
    task = await store.create("delivery-echo", max_attempts=3)
    await store.transition(
        task.task_id, TaskStatus.RUNNING, attempts=1, worker_id="dead-worker"
    )
    now += timedelta(seconds=120)
    queue.unavailable = True
    assert await sweep(store, executor) == 0
    pending = await store.require(task.task_id)
    assert pending.status is TaskStatus.PENDING
    assert pending.error is not None and pending.error.code == "lease_expired"
    queue.unavailable = False
    assert await sweep(store, executor) == 1
    [job] = queue.jobs.values()
    assert job[2] is None, "租约回收不是业务失败，不应额外加退避"


async def test_finished_transport_result_does_not_strand_pending_task() -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    task = await store.create("delivery-echo")
    job_id = executor.job_id_for(task.task_id, 0, LANE_CPU, version=task.version)
    # ARQ job 在认领前访问数据库失败，留下结果键而非队列消息。
    queue.results.add(result_key_prefix + job_id)
    unrelated = result_key_prefix + "unrelated:v0"
    queue.results.add(unrelated)
    assert await sweep(store, executor) == 1
    assert job_id in queue.jobs
    assert queue.results == {unrelated}
    assert (await store.require(task.task_id)).status is TaskStatus.PENDING


async def test_manual_retry_with_reset_attempts_gets_a_new_delivery_identity() -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    task = await store.create("delivery-echo")
    await executor.submit(task.task_id)
    old_job = next(iter(queue.jobs))
    queue.jobs.clear()
    queue.results.add(result_key_prefix + old_job)
    await store.transition(task.task_id, TaskStatus.RUNNING, attempts=1)
    await store.transition(task.task_id, TaskStatus.FAILED)
    pending = await TaskService(store, executor).retry(task.task_id)
    [new_job] = queue.jobs
    assert new_job != old_job
    assert pending.attempts == 0
    assert result_key_prefix + old_job in queue.results


async def test_obsolete_message_cannot_start_new_retry_or_change_backoff() -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    task = await store.create("delivery-stale")
    await executor.submit(task.task_id)
    old_job = next(iter(queue.jobs))
    pending = await store.update(task.task_id, attempts=1)
    calls = 0

    @executor.runners.register("delivery-stale")
    async def runner(ctx: TaskContext) -> Done:
        nonlocal calls
        calls += 1
        return Done(result="unexpected")

    try:
        await run_task({"executor": executor, "job_id": old_job}, task.task_id)
        assert calls == 0
        assert await store.require(task.task_id) == pending
    finally:
        await executor.shutdown()
        executor.runners.unregister("delivery-stale")


async def test_claim_rechecks_delivery_version_after_metadata_validation() -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    task = await store.create("delivery-echo")
    pending = await store.update(task.task_id, attempts=1)
    await executor._execute(task.task_id, expected_version=task.version)
    assert await store.require(task.task_id) == pending


async def test_reconciliation_rotates_bounded_pages_and_continues_after_failure() -> (
    None
):
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    tasks = [await store.create("delivery-echo") for _ in range(7)]
    newest = tasks[-1]
    queue.unavailable_ids.add(
        executor.job_id_for(newest.task_id, 0, LANE_CPU, version=newest.version)
    )
    assert await executor.reconcile_pending(limit=3) == 2
    queue.unavailable_ids.clear()
    assert await executor.reconcile_pending(limit=3) == 3
    assert await executor.reconcile_pending(limit=3) == 1
    assert await executor.reconcile_pending(limit=3) == 3
    assert len(queue.jobs) == 7
    assert {payload[0][0] for payload in queue.jobs.values()} == {
        task.task_id for task in tasks
    }


async def test_long_outage_retries_immediately_instead_of_using_a_negative_job_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    now = Time.now()
    monkeypatch.setattr(Time, "now", lambda: now)
    await store.create(
        "delivery-echo",
        attempts=2,
        error=TaskError(code="upstream_503", message="retry", retriable=True),
    )
    now += timedelta(days=2)
    await sweep(store, executor)
    [job] = queue.jobs.values()
    assert job[2] is None


@pytest.mark.parametrize("status", [TaskStatus.CANCELLED, TaskStatus.SUCCEEDED])
async def test_reconciliation_never_delivers_terminal_tasks(status: TaskStatus) -> None:
    store, queue = InMemoryTaskStore(), FakeQueue()
    executor = executor_for(store, queue)
    task = await store.create("delivery-echo")
    if status is TaskStatus.SUCCEEDED:
        await store.transition(task.task_id, TaskStatus.RUNNING)
    await store.transition(task.task_id, status)
    assert await sweep(store, executor) == 0
    assert queue.jobs == {}
