from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from comet_rag.core.time import Time
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.services.revision_maintenance import (
    RECONCILIATION_KIND,
    RevisionMaintenance,
)
from comet_rag.services.revision_reconciliation import (
    ReconciliationReport,
    RevisionReconciler,
)
from comet_rag.tasks import InMemoryTaskStore, TaskStatus
from comet_rag.tasks.store import TaskLeaseLost
from tests.unit.services.test_revision_indexing import FaultyDocuments, FaultyVectors

EMPTY = ReconciliationReport(0, (), (), (), (), True)


async def maintenance(store=None, *, resource="milvus:test:prefix", kind="source-kind"):
    store = store or InMemoryTaskStore()
    documents, vectors = FaultyDocuments(), FaultyVectors()
    operation = RevisionReconciler(
        task_store=store,
        document_store=documents,
        garbage_collector=RevisionGarbageCollector(
            document_store=documents, vector_store=vectors
        ),
        task_kind=kind,
        page_size=1,
    )
    service = RevisionMaintenance(
        task_store=store,
        reconciler=operation,
        task_kind=kind,
        resource_identity=resource,
    )
    await service.initialize()
    return service, store, operation


async def test_one_retained_cursor_is_reused_and_never_claims_business_success():
    first, store, _ = await maintenance()
    second, _, _ = await maintenance(store)
    before = await first.status()
    assert before["task_id"] == (await second.status())["task_id"]
    await first.run_once()
    await second.run_once()
    tasks = await store.list_tasks(kind=RECONCILIATION_KIND)
    assert len(tasks) == 1
    assert tasks[0].status is TaskStatus.PENDING and tasks[0].attempts == 2
    assert tasks[0].worker_id is None and tasks[0].error is None
    assert tasks[0].stage_history == []
    assert tasks[0].context == {
        "revision_scan": {"version": 1, "kind": "source-kind", "before": None}
    }
    assert tasks[0].result["cycle_complete"] is True


async def test_competing_contexts_only_one_page_can_run(monkeypatch):
    first, store, operation = await maintenance()
    second, _, _ = await maintenance(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(ctx):
        await ctx.checkpoint()
        entered.set()
        await release.wait()
        return EMPTY

    monkeypatch.setattr(operation, "run_page", blocked)
    running = asyncio.create_task(first.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 0.2)
        assert await asyncio.wait_for(second.run_once(), 0.2) is None
        assert (await store.list_tasks(kind=RECONCILIATION_KIND))[
            0
        ].status is TaskStatus.RUNNING
    finally:
        release.set()
        await running
    assert (await first.status())["status"] == "pending"


async def test_namespace_conflict_fails_without_a_second_cursor():
    first, store, operation = await maintenance()
    operation.run_page = AsyncMock(return_value=EMPTY)
    with pytest.raises(ValueError, match="命名空间"):
        await maintenance(store, resource="another-prefix")
    operation.run_page.assert_not_awaited()
    assert len(await store.list_tasks(kind=RECONCILIATION_KIND)) == 1
    assert (await first.status())["status"] == "pending"


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("offline"), TimeoutError("budget"), ValueError("cursor")],
)
async def test_page_failure_is_durable_and_next_tick_retries_without_reset(
    failure, monkeypatch
):
    service, store, operation = await maintenance()
    with monkeypatch.context() as patch:
        patch.setattr(operation, "run_page", AsyncMock(side_effect=failure))
        with pytest.raises(type(failure)):
            await service.run_once()
    state = await service.status()
    task = await store.require(state["task_id"])
    cursor = task.context.copy()
    assert task.status is TaskStatus.FAILED and task.attempts == 1
    assert state["error_code"] == "revision_reconciliation_failed"
    # 新触发重读同一持久化游标，不创建下一页 Task 或清空原 context。
    await service.run_once()
    current = await store.require(task.task_id)
    assert current.status is TaskStatus.PENDING and current.attempts == 2
    assert current.context == cursor and current.error is None


async def test_old_invocation_cannot_release_replacement():
    service, store, _ = await maintenance()
    task_id = (await service.status())["task_id"]
    old = await store.transition(
        task_id, TaskStatus.RUNNING, worker_id="old", attempts=1
    )
    await store.transition(task_id, TaskStatus.FAILED)
    await store.transition(task_id, TaskStatus.PENDING)
    replacement = await store.transition(
        task_id, TaskStatus.RUNNING, worker_id="new", attempts=2
    )
    with pytest.raises(TaskLeaseLost):
        await service._release(old, TaskStatus.PENDING, report=EMPTY)
    assert await store.require(task_id) == replacement


async def test_explicit_cancel_stops_and_does_not_create_replacement(monkeypatch):
    service, store, operation = await maintenance()

    async def cancel(ctx):
        await store.transition(ctx.task_id, TaskStatus.CANCELLING)
        await ctx.checkpoint()

    monkeypatch.setattr(operation, "run_page", cancel)
    assert await service.run_once() is None
    assert (await service.status())["status"] == "cancelled"
    assert await service.run_once() is None
    assert len(await store.list_tasks(kind=RECONCILIATION_KIND)) == 1


async def test_shutdown_interruption_is_recovered_by_existing_lease_sweep(monkeypatch):
    service, store, operation = await maintenance()
    entered = asyncio.Event()

    async def interrupted(ctx):
        await ctx.put_bounded(
            4096, revision_scan={"version": 1, "kind": "source-kind", "before": None}
        )
        entered.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patch:
        patch.setattr(operation, "run_page", interrupted)
        running = asyncio.create_task(service.run_once())
        await asyncio.wait_for(entered.wait(), 0.2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    task_id = (await service.status())["task_id"]
    snapshot = await store.require(task_id)
    assert snapshot.status is TaskStatus.RUNNING
    later = Time.now() + timedelta(seconds=200)
    monkeypatch.setattr(Time, "now", lambda: later)
    assert (await service.status())["overdue"] is True
    await store.sweep_stale(timedelta(seconds=90))
    assert (await store.require(task_id)).status is TaskStatus.FAILED
    fresh, _, _ = await maintenance(store)
    await fresh.run_once()
    assert (await store.require(task_id)).context == snapshot.context
    assert (await store.require(task_id)).attempts == 2


async def test_partial_page_failure_is_reported_and_logged(monkeypatch):
    from comet_rag.services import revision_maintenance as module

    service, store, operation = await maintenance()
    report = ReconciliationReport(1, (), (), (), ("bad-source",), False)
    monkeypatch.setattr(operation, "run_page", AsyncMock(return_value=report))
    error = Mock()
    monkeypatch.setattr(module.logger, "error", error)
    assert await service.run_once() == report
    status = await service.status()
    last_page = status["last_page"]
    assert isinstance(last_page, dict) and last_page["failed"] == ("bad-source",)
    assert status["status"] == "pending"
    error.assert_called_once()
    assert "revision_reconciliation_partial_failure" in error.call_args.args[0]


async def test_scope_is_rechecked_before_claim_after_context_was_created(monkeypatch):
    service, store, operation = await maintenance()
    task_id = (await service.status())["task_id"]
    await store.update(
        task_id, request={"version": 1, "kind": "other", "resource": "bad"}
    )
    spy = AsyncMock(return_value=EMPTY)
    monkeypatch.setattr(operation, "run_page", spy)
    with pytest.raises(ValueError, match="命名空间"):
        await service.run_once()
    spy.assert_not_awaited()


@pytest.mark.parametrize("committed", [False, True])
async def test_page_confirmation_loss_resumes_persisted_state(committed, monkeypatch):
    service, store, _ = await maintenance()
    original = store._transition_guarded

    async def lose(task_id, status, **kwargs):
        if status is TaskStatus.PENDING and kwargs.get("result") is not None:
            if committed:
                await original(task_id, status, **kwargs)
            raise ConnectionError("page confirmation lost")
        return await original(task_id, status, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_transition_guarded", lose)
        with pytest.raises((ConnectionError, TaskLeaseLost)):
            await service.run_once()
    [saved] = await store.list_tasks(kind=RECONCILIATION_KIND)
    assert saved.status is (TaskStatus.PENDING if committed else TaskStatus.FAILED)
    fresh, _, _ = await maintenance(store)
    await fresh.run_once()
    [current] = await store.list_tasks(kind=RECONCILIATION_KIND)
    assert current.task_id == saved.task_id and current.attempts == 2
    assert current.status is TaskStatus.PENDING


async def test_worker_pending_repair_never_enqueues_maintenance_cursor():
    from tests.unit.tasks.test_delivery_recovery import FakeQueue, executor_for, sweep

    _, store, _ = await maintenance()
    business = await store.create("delivery-echo")
    queue = FakeQueue()
    executor = executor_for(store, queue)
    try:
        assert await sweep(store, executor) == 1
        assert [payload[0] for payload in queue.jobs.values()] == [(business.task_id,)]
        [cursor] = await store.list_tasks(kind=RECONCILIATION_KIND)
        assert cursor.status is TaskStatus.PENDING and cursor.attempts == 0
    finally:
        await executor.shutdown()


async def test_claims_and_heartbeats_do_not_hide_missing_page_progress(monkeypatch):
    service, store, _ = await maintenance()
    task_id = (await service.status())["task_id"]
    later = Time.now() + timedelta(seconds=200)
    monkeypatch.setattr(Time, "now", lambda: later)
    await store.update(task_id, message="recent retry")
    await store.heartbeat(task_id)
    assert (await service.status())["overdue"] is True
    await service.run_once()
    assert (await service.status())["overdue"] is False


async def test_concurrent_pending_snapshots_are_claimed_with_version_cas(monkeypatch):
    first, store, operation = await maintenance()
    second, _, other_operation = await maintenance(store)
    page = AsyncMock(return_value=EMPTY)
    monkeypatch.setattr(operation, "run_page", page)
    monkeypatch.setattr(other_operation, "run_page", page)
    transition = store.transition
    both = asyncio.Event()
    arrivals = 0

    async def barrier(task_id, status, **fields):
        nonlocal arrivals
        if status is TaskStatus.RUNNING:
            arrivals += 1
            if arrivals == 2:
                both.set()
            await both.wait()
        return await transition(task_id, status, **fields)

    monkeypatch.setattr(store, "transition", barrier)
    await asyncio.wait_for(asyncio.gather(first.run_once(), second.run_once()), 0.5)
    page.assert_awaited_once()
    [cursor] = await store.list_tasks(kind=RECONCILIATION_KIND)
    assert cursor.attempts == 1 and cursor.status is TaskStatus.PENDING


async def test_boolean_scope_version_is_not_an_integer_version():
    service, store, _ = await maintenance()
    task_id = (await service.status())["task_id"]
    task = await store.require(task_id)
    request = {**task.request, "version": True}
    await store.update(task_id, request=request)
    with pytest.raises(ValueError, match="命名空间"):
        await service.run_once()
