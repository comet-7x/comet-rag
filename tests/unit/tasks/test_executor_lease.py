from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from comet_rag.core.time import Time
from comet_rag.tasks import (
    Done,
    InMemoryTaskStore,
    InProcessExecutor,
    TaskContext,
    TaskService,
    TaskStatus,
)
from tests.contracts.support import wait_for_terminal, wait_until


async def test_long_stage_keeps_lease_without_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTaskStore()
    executor = InProcessExecutor(store)
    executor._heartbeat_interval = 0.005
    started, release = asyncio.Event(), asyncio.Event()
    now = Time.now()
    monkeypatch.setattr(Time, "now", lambda: now)

    @executor.runners.register("lease-long-stage")
    async def runner(ctx: TaskContext) -> Done:
        started.set()
        await release.wait()
        return Done(result="complete")

    try:
        task = await TaskService(store, executor).submit(
            "lease-long-stage", max_attempts=3
        )
        await asyncio.wait_for(started.wait(), 1.0)
        now += timedelta(seconds=120)

        async def renewed() -> bool:
            return (await store.require(task.task_id)).heartbeat_at == now

        await wait_until(renewed, timeout=1.0)
        assert await store.sweep_stale(timedelta(seconds=90)) == []
        release.set()
        done = await wait_for_terminal(store, task.task_id)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.attempts == 1
        assert done.result == "complete"
        await executor.shutdown()
        await asyncio.sleep(0.015)
        assert (await store.require(task.task_id)).heartbeat_at is None
    finally:
        release.set()
        await executor.shutdown()
        executor.runners.unregister("lease-long-stage")


@pytest.mark.parametrize(
    ("worker_id", "attempts"), [("replacement", 1), ("lease-worker", 2)]
)
async def test_takeover_stops_old_runner_without_changing_new_owner(
    worker_id: str, attempts: int
) -> None:
    store = InMemoryTaskStore()
    executor = InProcessExecutor(store, worker_id="lease-worker")
    executor._heartbeat_interval = 0.005
    started, stopped = asyncio.Event(), asyncio.Event()

    @executor.runners.register("lease-takeover")
    async def runner(ctx: TaskContext) -> Done:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return Done(result="stale")

    try:
        task = await TaskService(store, executor).submit("lease-takeover")
        await asyncio.wait_for(started.wait(), 1.0)
        replacement = await store.update(
            task.task_id, worker_id=worker_id, attempts=attempts
        )
        await asyncio.wait_for(stopped.wait(), 1.0)
        await asyncio.wait_for(executor.wait_all(), 1.0)
        await executor.shutdown()
        current = await store.require(task.task_id)
        assert current.status is TaskStatus.RUNNING
        assert current.worker_id == worker_id
        assert current.attempts == attempts
        assert current.version == replacement.version
        assert current.heartbeat_at == replacement.heartbeat_at
        assert current.result is None
    finally:
        await executor.shutdown()
        executor.runners.unregister("lease-takeover")


async def test_renewal_failure_stops_runner_before_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTaskStore()
    executor = InProcessExecutor(store)
    executor._heartbeat_interval = 0.005
    stopped = asyncio.Event()

    async def failing_renewal(task_id: str, worker_id: str, attempts: int) -> None:
        raise ConnectionError("lease storage unavailable")

    monkeypatch.setattr(store, "_renew_lease", failing_renewal)

    @executor.runners.register("lease-failure")
    async def runner(ctx: TaskContext) -> Done:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return Done(result="unexpected")

    try:
        task = await TaskService(store, executor).submit("lease-failure")
        done = await wait_for_terminal(store, task.task_id)
        assert stopped.is_set()
        assert done.status is TaskStatus.FAILED
        assert done.result is None
    finally:
        await executor.shutdown()
        executor.runners.unregister("lease-failure")
