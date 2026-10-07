from __future__ import annotations

from dataclasses import replace

import pytest

from comet_rag.tasks import InMemoryTaskStore, Task, TaskContext, TaskStatus
from comet_rag.tasks.runner import TaskContextTooLarge
from comet_rag.tasks.store import TaskLeaseLost


async def test_budget_counts_utf8_bytes_and_failed_write_preserves_context() -> None:
    store = InMemoryTaskStore()
    task = await store.create("budget")
    ctx = TaskContext(store, task.task_id)
    with pytest.raises(TaskContextTooLarge):
        await ctx.put_bounded(10, a="中")
    assert (await ctx.snapshot()).context == {}
    await ctx.put_bounded(11, a="中")
    with pytest.raises(TaskContextTooLarge):
        await ctx.put_bounded(11, b="x")
    assert (await ctx.snapshot()).context == {"a": "中"}


@pytest.mark.parametrize("budget", [0, -1, True, None])
async def test_budget_must_be_positive_strict_integer(budget) -> None:
    store = InMemoryTaskStore()
    task = await store.create("budget")
    with pytest.raises(ValueError):
        await TaskContext(store, task.task_id).put_bounded(budget, a=1)


@pytest.mark.parametrize("value", [float("nan"), object()])
async def test_bounded_context_requires_finite_json(value) -> None:
    store = InMemoryTaskStore()
    task = await store.create("budget")
    with pytest.raises((TypeError, ValueError)):
        await TaskContext(store, task.task_id).put_bounded(1000, a=value)
    assert (await store.require(task.task_id)).context == {}


class ConcurrentWriteStore(InMemoryTaskStore):
    inject = False

    async def _save(
        self, task: Task, expected_version: int, *, bump: bool = True
    ) -> Task:
        if self.inject:
            self.inject = False
            current = await self.require(task.task_id)
            await super()._save(
                replace(current, context={"concurrent": "x" * 100}), expected_version
            )
        return await super()._save(task, expected_version, bump=bump)


async def test_budget_rechecks_after_cas_conflict_without_erasing_concurrent_write() -> (
    None
):
    store = ConcurrentWriteStore()
    task = await store.create("budget")
    store.inject = True
    with pytest.raises(TaskContextTooLarge):
        await TaskContext(store, task.task_id).put_bounded(50, own="small")
    assert (await store.require(task.task_id)).context == {"concurrent": "x" * 100}


async def test_bounded_put_keeps_execution_ownership_fence() -> None:
    store = InMemoryTaskStore()
    task = await store.create("budget")
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="replacement", attempts=2
    )
    stale = TaskContext(store, task.task_id, worker_id="old", attempts=1)
    with pytest.raises(TaskLeaseLost):
        await stale.put_bounded(1000, a=1)
    assert (await stale.snapshot()).context == {}
