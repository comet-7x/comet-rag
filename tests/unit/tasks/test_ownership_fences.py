from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

import pytest

from comet_rag.core.time import Time
from comet_rag.infrastructure.task_execution.arq import ArqExecutor
from comet_rag.tasks import (
    Done,
    Handoff,
    InMemoryTaskStore,
    InProcessExecutor,
    RetriableError,
    Task,
    TaskContext,
    TaskService,
    TaskStatus,
    register,
)
from comet_rag.tasks.runner import TaskCancelled, unregister
from comet_rag.tasks.store import TaskLeaseLost


class TakeoverDuringSave(InMemoryTaskStore):
    """在检查所有权之后、CAS 保存之前接管，强制触发旧执行者的重读。"""

    def __init__(self) -> None:
        super().__init__()
        self.armed: TaskStatus | None = None
        self.replacement: Task | None = None

    async def _save(
        self, task: Task, expected_version: int, *, bump: bool = True
    ) -> Task:
        if self.armed is task.status:
            self.armed = None
            current = await self.require(task.task_id)
            self.replacement = await self.update(
                task.task_id, worker_id="replacement", attempts=current.attempts + 1
            )
        return await super()._save(task, expected_version, bump=bump)


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "retry", "handoff", "cancel"]
)
async def test_takeover_during_final_save_cannot_be_overwritten(outcome: str) -> None:
    store = TakeoverDuringSave()
    executor = InProcessExecutor(store, lane="cpu", retry_backoff=0)
    kind = f"fence-{outcome}"

    @register(kind)
    async def runner(ctx: TaskContext) -> Done | Handoff:
        if store.replacement is not None:
            return Done(result="unexpected second run")
        await ctx.enter_stage("working")
        store.armed = {
            "success": TaskStatus.SUCCEEDED,
            "failure": TaskStatus.FAILED,
            "retry": TaskStatus.PENDING,
            "handoff": TaskStatus.PENDING,
            "cancel": TaskStatus.CANCELLED,
        }[outcome]
        if outcome == "failure":
            raise ValueError("old failure")
        if outcome == "retry":
            raise RetriableError("old retry")
        if outcome == "cancel":
            raise TaskCancelled(ctx.task_id)
        if outcome == "handoff":
            return Handoff(lane="io", resume_stage="next")
        return Done(result="old result")

    try:
        task = await TaskService(store, executor).submit(kind, max_attempts=3)
        await asyncio.wait_for(executor.wait_all(), 1.0)
        assert store.replacement is not None
        assert await store.require(task.task_id) == store.replacement
        await executor.shutdown()
        assert await store.require(task.task_id) == store.replacement
    finally:
        await executor.shutdown()
        unregister(kind)


async def test_same_worker_new_attempt_finishes_before_next_heartbeat() -> None:
    store = InMemoryTaskStore()
    executor = InProcessExecutor(store, worker_id="reused-worker")
    replacement: Task | None = None

    @register("fence-fast-reuse")
    async def runner(ctx: TaskContext) -> Done:
        nonlocal replacement
        replacement = await store.update(ctx.task_id, attempts=2)
        return Done(result="old attempt")

    try:
        task = await TaskService(store, executor).submit("fence-fast-reuse")
        await executor.wait_all()
        assert replacement is not None
        assert await store.require(task.task_id) == replacement
    finally:
        await executor.shutdown()
        unregister("fence-fast-reuse")


@pytest.mark.parametrize("write", ["put", "report", "enter_stage"])
async def test_context_write_rechecks_ownership_inside_cas(write: str) -> None:
    store = TakeoverDuringSave()
    task = await store.create("fence-context", context={"preserved": True})
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="old", attempts=1
    )
    ctx = TaskContext(store, task.task_id, worker_id="old", attempts=1)
    store.armed = TaskStatus.RUNNING
    with pytest.raises(TaskLeaseLost):
        if write == "put":
            await ctx.put(stale=True)
        elif write == "report":
            await ctx.report(progress=0.9, message="stale")
        else:
            await ctx.enter_stage("stale")
    assert store.replacement is not None
    assert await store.require(task.task_id) == store.replacement


async def test_reclaim_rechecks_heartbeat_after_candidate_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTaskStore()
    now = Time.now()
    monkeypatch.setattr(Time, "now", lambda: now)
    task = await store.create("fence-sweep", max_attempts=3)
    candidate = await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="healthy", attempts=1
    )
    now += timedelta(seconds=120)
    await store.heartbeat(task.task_id)
    healthy = await store.require(task.task_id)
    original = store._stale_candidates

    async def stale_snapshot(lease: timedelta, at):
        monkeypatch.setattr(store, "_stale_candidates", original)
        return [candidate]

    monkeypatch.setattr(store, "_stale_candidates", stale_snapshot)
    assert await store.sweep_stale(timedelta(seconds=90)) == []
    assert await store.require(task.task_id) == healthy


@pytest.mark.parametrize("race", ["claim", "complete"])
async def test_explicit_cancel_recomputes_status_after_a_race(
    race: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTaskStore()
    executor = InProcessExecutor(store)
    task = await store.create("fence-cancel-race")
    if race == "complete":
        await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="worker")
    original_save = store._save
    raced = False

    async def save(
        candidate: Task, expected_version: int, *, bump: bool = True
    ) -> Task:
        nonlocal raced
        if not raced:
            raced = True
            if race == "claim":
                await store.transition(
                    task.task_id, TaskStatus.RUNNING, worker_id="worker"
                )
            else:
                await store.transition(
                    task.task_id, TaskStatus.SUCCEEDED, result="completed"
                )
        return await original_save(candidate, expected_version, bump=bump)

    monkeypatch.setattr(store, "_save", save)
    try:
        accepted = await executor.request_cancel(task.task_id)
        current = await store.require(task.task_id)
        if race == "claim":
            assert accepted is True
            assert current.status is TaskStatus.CANCELLING
            assert current.worker_id == "worker"
        else:
            assert accepted is False
            assert current.status is TaskStatus.SUCCEEDED
            assert current.result == "completed"
    finally:
        await executor.shutdown()


async def test_old_handoff_cleanup_does_not_cancel_a_new_claim_with_same_attempt() -> (
    None
):
    store = InMemoryTaskStore()
    started, release = asyncio.Event(), asyncio.Event()
    next_job: asyncio.Task[None] | None = None

    class OverlappingHandoff(ArqExecutor):
        async def _schedule_handoff(self, task_id: str, lane: str) -> None:
            nonlocal next_job
            # 模拟队列迅速认领下一段，同时上一段 job 在投递后被取消。
            next_job = asyncio.create_task(self.execute(task_id))
            await started.wait()
            raise asyncio.CancelledError

    executor = OverlappingHandoff(store, worker_id="same-process")

    @register("fence-handoff-overlap")
    async def runner(ctx: TaskContext) -> Done | Handoff:
        task = await ctx.snapshot()
        if task.resume_stage is None:
            return Handoff(lane="io", resume_stage="next")
        await ctx.put(new_claim=True)
        started.set()
        await release.wait()
        return Done(result="new result")

    try:
        task = await store.create("fence-handoff-overlap", max_attempts=1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(executor.execute(task.task_id), 1.0)
        await executor.wait_all()
        current = await store.require(task.task_id)
        assert current.status is TaskStatus.RUNNING
        assert current.attempts == 1
        assert current.context == {"new_claim": True}
        release.set()
        assert next_job is not None
        await asyncio.wait_for(next_job, 1.0)
        assert (await store.require(task.task_id)).result == "new result"
    finally:
        release.set()
        if next_job is not None:
            await asyncio.wait_for(next_job, 1.0)
        await executor.shutdown()
        unregister("fence-handoff-overlap")


async def test_duplicate_delivery_does_not_drop_the_running_claim() -> None:
    store = InMemoryTaskStore()
    executor = ArqExecutor(store)
    started = asyncio.Event()

    @register("fence-duplicate")
    async def runner(ctx: TaskContext) -> Done:
        started.set()
        while True:
            await ctx.checkpoint()
            await asyncio.sleep(0.001)

    task = await store.create("fence-duplicate")
    job = asyncio.create_task(executor.execute(task.task_id))
    try:
        await asyncio.wait_for(started.wait(), 1.0)
        await executor.execute(task.task_id)
        await executor.shutdown(timeout=1.0)
        assert (await store.require(task.task_id)).status is TaskStatus.CANCELLED
        await job
    finally:
        job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job
        await executor.shutdown()
        unregister("fence-duplicate")
