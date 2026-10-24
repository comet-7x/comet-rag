from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from comet_rag.services.revision_maintenance import RevisionMaintenance
from comet_rag.services.revision_reconciliation import (
    ReconciliationDisposition,
    RevisionTaskRetained,
    guard_revision_task_deletion,
)
from comet_rag.tasks import TaskService, TaskStatus
from tests.unit.services.test_revision_reconciliation import (
    completed_environment,
    reconciler,
    scan_context,
)


@pytest.mark.parametrize(
    "status", [TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SUCCEEDED]
)
async def test_missing_history_is_explicit_and_cannot_authorize_any_store_io(
    status, monkeypatch
):
    ctx, documents, vectors, _ = await completed_environment()
    before = await ctx.snapshot()
    # 保留真实 key 和原文；它们仍不能证明原 plan 指纹/计数/任务归属。
    history = await ctx.store.create(
        "revision-test",
        context={
            "revision_key": before.context["revision_key"],
            "text": "historical input",
        },
    )
    if status is TaskStatus.SUCCEEDED:
        await ctx.store.transition(history.task_id, TaskStatus.RUNNING)
    await ctx.store.transition(history.task_id, status)
    history = await ctx.store.require(history.task_id)
    reserve, abandon, delete = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(documents, "reserve_revision", reserve)
    monkeypatch.setattr(documents, "abandon_revision", abandon)
    monkeypatch.setattr(vectors, "delete", delete)
    result = await reconciler(ctx.store, documents, vectors).reconcile(history)
    assert result is ReconciliationDisposition.MISSING_PAYLOAD
    reserve.assert_not_awaited()
    abandon.assert_not_awaited()
    delete.assert_not_awaited()
    assert await ctx.store.require(history.task_id) == history
    executor = Mock(request_cancel=AsyncMock())
    service = TaskService(
        ctx.store, executor, delete_guard=guard_revision_task_deletion
    )
    with pytest.raises(RevisionTaskRetained):
        await service.delete(history.task_id, force=True)
    executor.request_cancel.assert_not_awaited()


async def test_missing_history_is_not_an_ordinary_deferral_or_page_failure(monkeypatch):
    from comet_rag.services import revision_maintenance as module

    ctx, documents, vectors, _ = await completed_environment()
    key = (await ctx.snapshot()).context["revision_key"]
    missing = await ctx.store.create("revision-test", context={"revision_key": key})
    await ctx.store.transition(missing.task_id, TaskStatus.CANCELLED)
    operation = reconciler(ctx.store, documents, vectors, page_size=2)
    monitor = RevisionMaintenance(
        task_store=ctx.store,
        reconciler=operation,
        task_kind="revision-test",
        resource_identity="test:original-namespace",
    )
    await monitor.initialize()
    error = Mock()
    monkeypatch.setattr(module.logger, "error", error)
    first = await monitor.run_once()
    assert first is not None
    assert first.scanned == 2 and not first.cycle_complete
    assert first.missing_payload == (missing.task_id,)
    assert first.cleaned == (ctx.task_id,)
    assert first.deferred == first.failed == first.ignored == ()
    status = await monitor.status()
    assert status["status"] == "pending" and status["error_code"] is None
    last_page = status["last_page"]
    assert isinstance(last_page, dict)
    assert last_page["missing_payload"] == (missing.task_id,)
    error.assert_called_once()
    assert "revision_reconciliation_recovery_required" in error.call_args.args[0]
    # 这是最近一页，不能被当作历史总数；下一轮仍须再次发现并保留同一坏记录。
    tail = await monitor.run_once()
    assert tail is not None and tail.cycle_complete
    repeated = await monitor.run_once()
    assert repeated is not None
    assert repeated.missing_payload == (missing.task_id,)


@pytest.mark.parametrize(
    "context",
    [
        {"revision_key": None},
        {"revision_key": {"partial": True}},
        {"revision_publication": None},
    ],
)
async def test_unverifiable_identity_is_retained_without_inventing_a_draft(context):
    ctx, documents, vectors, _ = await completed_environment()
    source = await ctx.store.create("revision-test", context=context)
    await ctx.store.transition(source.task_id, TaskStatus.FAILED)
    scan = await scan_context(ctx.store)
    report = await reconciler(ctx.store, documents, vectors, page_size=2).run_page(scan)
    assert report.missing_payload == (source.task_id,)
    assert report.cleaned == (ctx.task_id,)
    assert report.failed == ()
    assert (await ctx.store.require(source.task_id)).context == context


@pytest.mark.parametrize("payload", [{}, {"version": True, "draft": {}}, "broken"])
async def test_present_but_damaged_payload_is_failed_and_not_silently_missing(payload):
    ctx, documents, vectors, _ = await completed_environment()
    source = await ctx.store.create(
        "revision-test", context={"revision_prepare": payload}
    )
    await ctx.store.transition(source.task_id, TaskStatus.CANCELLED)
    scan = await scan_context(ctx.store)
    report = await reconciler(ctx.store, documents, vectors, page_size=2).run_page(scan)
    assert report.failed == (source.task_id,)
    assert report.missing_payload == report.deferred == ()
    assert report.cleaned == (ctx.task_id,)


async def test_running_history_still_defers_and_does_not_raise_a_recovery_alarm():
    ctx, documents, vectors, _ = await completed_environment()
    source = await ctx.store.create("revision-test", context={"revision_key": None})
    assert (
        await reconciler(ctx.store, documents, vectors).reconcile(source)
        is ReconciliationDisposition.DEFERRED
    )


async def test_tail_report_keeps_missing_payload_when_resetting_the_cursor():
    ctx, documents, vectors, _ = await completed_environment()
    source = await ctx.store.create("revision-test", context={"revision_key": None})
    await ctx.store.transition(source.task_id, TaskStatus.CANCELLED)
    scan = await scan_context(ctx.store)
    report = await reconciler(ctx.store, documents, vectors, page_size=3).run_page(scan)
    assert report.cycle_complete and report.scanned == 2
    assert report.missing_payload == (source.task_id,)
    assert report.cleaned == (ctx.task_id,)
    assert (await scan.snapshot()).context["revision_scan"]["before"] is None
