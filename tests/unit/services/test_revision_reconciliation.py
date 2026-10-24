from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from comet_rag.ports import VectorRecord
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.services.revision_reconciliation import (
    ReconciliationDisposition,
    RevisionReconciler,
    RevisionTaskRetained,
    guard_revision_task_deletion,
)
from comet_rag.tasks import InMemoryTaskStore, TaskContext, TaskService, TaskStatus
from comet_rag.tasks.runner import TaskCancelled
from comet_rag.tasks.store import TaskLeaseLost
from tests.unit.services.test_revision_publication import prepared


async def test_offset_scan_repeats_a_page_when_new_rows_arrive_at_scan_speed() -> None:
    store = InMemoryTaskStore()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    old = [
        await store.create("revision", created_at=base + timedelta(seconds=i))
        for i in range(4)
    ]
    seen = []
    for page in range(3):
        rows = await store.list_tasks(kind="revision", limit=2, offset=page * 2)
        seen.append([task.task_id for task in rows])
        for index in range(2):
            await store.create(
                "revision", created_at=base + timedelta(seconds=4 + page * 2 + index)
            )
    # 新增两条与 offset 同速前移，每页都读到相同身份，最老两条一直没被扫描。
    assert seen == [[old[3].task_id, old[2].task_id]] * 3
    assert old[0].task_id not in {task_id for page in seen for task_id in page}


async def test_raw_task_deletion_loses_the_only_discoverable_purged_cleanup_intent() -> (
    None
):
    ctx, documents, vectors, _, stages = await prepared()
    outcome = await stages.activate(ctx)
    await stages.cleanup(ctx)
    await ctx.store.transition(ctx.task_id, TaskStatus.SUCCEEDED)
    assert await documents.list_cleanup_candidates(limit=32) == []
    assert await ctx.store.delete(ctx.task_id)
    await vectors.upsert(
        "kb",
        [
            VectorRecord(
                "late",
                "late",
                [1.0, 0.0],
                {
                    "source_id": "source",
                    "document_revision": outcome.revision.key.revision_id,
                },
            )
        ],
    )
    assert await ctx.store.list_tasks(kind="revision-test") == []
    assert (
        await vectors.count(
            "kb", filter={"document_revision": outcome.revision.key.revision_id}
        )
        == 1
    )
    assert await documents.list_cleanup_candidates(limit=32) == []


async def scan_context(store):
    task = await store.create("revision-scan")
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="scanner", attempts=1
    )
    return TaskContext(store, task.task_id, worker_id="scanner", attempts=1)


def reconciler(store, documents, vectors, **kwargs):
    return RevisionReconciler(
        task_store=store,
        document_store=documents,
        garbage_collector=RevisionGarbageCollector(
            document_store=documents, vector_store=vectors
        ),
        task_kind="revision-test",
        **kwargs,
    )


async def completed_environment():
    ctx, documents, vectors, model, stages = await prepared()
    await stages.activate(ctx)
    await stages.cleanup(ctx)
    await ctx.store.transition(ctx.task_id, TaskStatus.SUCCEEDED)
    return ctx, documents, vectors, model


async def test_repeated_pages_compensate_late_write_without_mutating_source_task() -> (
    None
):
    ctx, documents, vectors, _ = await completed_environment()
    scan = await scan_context(ctx.store)
    operation = reconciler(ctx.store, documents, vectors, page_size=1)
    before = await ctx.snapshot()
    first = await operation.run_page(scan)
    assert first.cleaned == (ctx.task_id,) and not first.cycle_complete
    key = before.context["revision_key"]
    await vectors.upsert(
        "kb",
        [
            VectorRecord(
                "late",
                "late",
                [1.0, 0.0],
                {
                    "source_id": "source",
                    "document_revision": key["revision_id"],
                },
            )
        ],
    )
    fresh = TaskContext(ctx.store, scan.task_id, worker_id="scanner", attempts=1)
    tail = await reconciler(ctx.store, documents, vectors, page_size=1).run_page(fresh)
    assert tail.scanned == 0 and tail.cycle_complete
    again = await operation.run_page(fresh)
    assert again.cleaned == (ctx.task_id,)
    assert (
        await vectors.count("kb", filter={"document_revision": key["revision_id"]}) == 0
    )
    assert await vectors.count("kb", filter={"document_revision": "winner"}) == 4
    assert await ctx.snapshot() == before


async def test_keyset_pages_reach_old_rows_under_continuous_head_insertions() -> None:
    ctx, documents, vectors, _ = await completed_environment()
    # 已存在的最老记录不能因新到头部任务而一直到不了。
    for _ in range(2):
        await ctx.store.create("revision-test")
    scan = await scan_context(ctx.store)
    operation = reconciler(ctx.store, documents, vectors, page_size=1)
    seen = []
    for _ in range(3):
        report = await operation.run_page(scan)
        seen.extend((*report.cleaned, *report.ignored))
        for _ in range(2):
            await ctx.store.create("revision-test")
    assert len(set(seen)) == 3 and ctx.task_id in seen


@pytest.mark.parametrize("after_commit", [False, True])
async def test_cursor_ack_failure_recovers_from_persisted_position(
    after_commit, monkeypatch
) -> None:
    ctx, documents, vectors, _ = await completed_environment()
    scan = await scan_context(ctx.store)
    operation = reconciler(ctx.store, documents, vectors, page_size=1)
    put = scan.put_bounded

    async def fail(limit, **values):
        if values["revision_scan"]["before"] is not None:
            if after_commit:
                await put(limit, **values)
            raise ConnectionError("cursor acknowledgment lost")
        await put(limit, **values)

    with monkeypatch.context() as patch:
        patch.setattr(scan, "put_bounded", fail)
        with pytest.raises(ConnectionError):
            await operation.run_page(scan)
    saved = (await scan.snapshot()).context["revision_scan"]["before"]
    assert (saved is not None) is after_commit
    fresh = TaskContext(ctx.store, scan.task_id, worker_id="scanner", attempts=1)
    report = await operation.run_page(fresh)
    assert report.scanned == (0 if after_commit else 1)
    assert await documents.list_cleanup_candidates(limit=32) == []


async def test_one_failure_does_not_block_other_records_and_is_revisited_next_cycle(
    monkeypatch,
) -> None:
    ctx, documents, vectors, _ = await completed_environment()
    # 将坏项放在头部，确保后面的正常记录仍能被扫描。
    bad = await ctx.store.create(
        "revision-test", context={"revision_key": {}, "revision_index": {"bad": True}}
    )
    await ctx.store.transition(bad.task_id, TaskStatus.FAILED)
    scan = await scan_context(ctx.store)
    operation = reconciler(ctx.store, documents, vectors, page_size=3)
    first = await operation.run_page(scan)
    assert (
        first.failed == (bad.task_id,)
        and first.cleaned == (ctx.task_id,)
        and first.cycle_complete
    )
    second = await operation.run_page(scan)
    assert (
        second.failed == (bad.task_id,) and await ctx.store.get(bad.task_id) is not None
    )


@pytest.mark.parametrize("boundary", ["before-query", "after-abandon"])
@pytest.mark.parametrize("fault", ["cancel", "lease"])
async def test_scan_ownership_loss_does_not_advance_current_item(
    boundary, fault, monkeypatch
) -> None:
    ctx, documents, vectors, _ = await completed_environment()
    scan = await scan_context(ctx.store)
    operation = reconciler(ctx.store, documents, vectors, page_size=1)
    query = ctx.store.list_tasks_before
    abandon = documents.abandon_revision

    async def lose():
        await scan.store.transition(
            scan.task_id,
            TaskStatus.CANCELLING if fault == "cancel" else TaskStatus.PENDING,
        )

    async def bad_query(**kwargs):
        await lose()
        return await query(**kwargs)

    async def bad_abandon(key):
        await abandon(key)
        await lose()

    with monkeypatch.context() as patch:
        delete = AsyncMock(wraps=vectors.delete)
        patch.setattr(vectors, "delete", delete)
        patch.setattr(
            ctx.store, "list_tasks_before", bad_query
        ) if boundary == "before-query" else patch.setattr(
            documents, "abandon_revision", bad_abandon
        )
        with pytest.raises(TaskCancelled if fault == "cancel" else TaskLeaseLost):
            await operation.run_page(scan)
        delete.assert_not_awaited()
    assert (await scan.snapshot()).context["revision_scan"]["before"] is None


async def test_timeout_preserves_interrupted_item_for_retry(monkeypatch) -> None:
    ctx, documents, vectors, _ = await completed_environment()
    scan = await scan_context(ctx.store)
    operation = reconciler(
        ctx.store, documents, vectors, page_size=1, timeout_seconds=0.01
    )
    wait = asyncio.Event()

    async def blocked(*args, **kwargs):
        await wait.wait()

    monkeypatch.setattr(vectors, "delete", blocked)
    running = asyncio.create_task(operation.run_page(scan))
    try:
        done, _ = await asyncio.wait({running}, timeout=0.2)
        assert running in done, "服务自己的工作预算没有终止阻塞 IO"
        with pytest.raises(TimeoutError):
            await running
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert (await scan.snapshot()).context["revision_scan"]["before"] is None
    assert await ctx.store.get(ctx.task_id) is not None


@pytest.mark.parametrize(
    "state",
    [TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.SUCCEEDED, TaskStatus.CANCELLED],
)
async def test_state_policy_preserves_active_and_retryable_unpublished_work(
    state,
) -> None:
    ctx, documents, vectors, _, stages = await prepared(superseded=False)
    if state is TaskStatus.CANCELLED:
        await ctx.store.transition(ctx.task_id, TaskStatus.CANCELLING)
    if state is not TaskStatus.RUNNING:
        await ctx.store.transition(ctx.task_id, state)
    result = await reconciler(ctx.store, documents, vectors).reconcile(
        await ctx.snapshot()
    )
    assert result is (
        ReconciliationDisposition.CLEANED
        if state is TaskStatus.CANCELLED
        else ReconciliationDisposition.DEFERRED
    )
    assert await vectors.count("kb") == (0 if state is TaskStatus.CANCELLED else 4)
    assert await documents.get_active_revisions("kb", ["source"]) == {"source": None}


async def test_terminal_active_and_activation_race_are_not_deleted(monkeypatch) -> None:
    ctx, documents, vectors, _, stages = await prepared(superseded=False)
    await stages.activate(ctx)
    await ctx.store.transition(ctx.task_id, TaskStatus.SUCCEEDED)
    operation = reconciler(ctx.store, documents, vectors)
    assert (
        await operation.reconcile(await ctx.snapshot())
        is ReconciliationDisposition.DEFERRED
    )
    # 即使读取时认为旧版本可清理，也由 atomic abandon 再次保护当前事实。
    old, docs, vec, _, _ = await prepared(superseded=False)
    await old.store.transition(old.task_id, TaskStatus.CANCELLING)
    await old.store.transition(old.task_id, TaskStatus.CANCELLED)
    abandon = docs.abandon_revision
    key = (await old.snapshot()).context["revision_key"]

    async def published_first(revision):
        await docs.activate_revision(revision)
        await abandon(revision)

    monkeypatch.setattr(docs, "abandon_revision", published_first)
    delete = AsyncMock(wraps=vec.delete)
    monkeypatch.setattr(vec, "delete", delete)
    assert (
        await reconciler(old.store, docs, vec).reconcile(await old.snapshot())
        is ReconciliationDisposition.DEFERRED
    )
    delete.assert_not_awaited()
    assert await docs.get_active_revisions("kb", ["source"]) == {
        "source": key["revision_id"]
    }


@pytest.mark.parametrize(
    "marker",
    [
        "revision_key",
        "revision_prepare",
        "revision_index",
        "revision_publication",
        "revision_scan",
    ],
)
@pytest.mark.parametrize("force", [False, True])
async def test_configured_task_service_retains_intent_even_if_invalid_or_complete(
    marker, force
) -> None:
    store = InMemoryTaskStore()
    task = await store.create("guarded", context={marker: None})
    executor = Mock()
    executor.request_cancel = AsyncMock()
    service = TaskService(store, executor, delete_guard=guard_revision_task_deletion)
    with pytest.raises(RevisionTaskRetained):
        await service.delete(task.task_id, force=force)
    executor.request_cancel.assert_not_awaited()
    assert await store.get(task.task_id) is not None


async def test_ordinary_task_is_deletable_and_anonymous_scan_is_rejected() -> None:
    ctx, documents, vectors, _ = await completed_environment()
    task = await ctx.store.create("ordinary")
    executor = Mock(request_cancel=AsyncMock())
    assert await TaskService(
        ctx.store, executor, delete_guard=guard_revision_task_deletion
    ).delete(task.task_id)
    scan = await scan_context(ctx.store)
    with pytest.raises(ValueError, match="所有权围栏"):
        await reconciler(ctx.store, documents, vectors).run_page(
            TaskContext(ctx.store, scan.task_id)
        )


async def test_missing_handoff_is_reported_without_reserving_or_guessing_a_plan(
    monkeypatch,
) -> None:
    ctx, documents, vectors, _ = await completed_environment()
    task = await ctx.store.create(
        "revision-test", context={"revision_key": {"partial": True}}
    )
    await ctx.store.transition(task.task_id, TaskStatus.CANCELLED)
    reserve = AsyncMock(wraps=documents.reserve_revision)
    monkeypatch.setattr(documents, "reserve_revision", reserve)
    assert (
        await reconciler(ctx.store, documents, vectors).reconcile(
            await ctx.store.require(task.task_id)
        )
        is ReconciliationDisposition.MISSING_PAYLOAD
    )
    reserve.assert_not_awaited()


@pytest.mark.parametrize("fault", ["version", "scope", "naive-time", "extra"])
async def test_bad_cursor_and_oversized_scan_context_fail_before_query(
    fault, monkeypatch
) -> None:
    ctx, documents, vectors, _ = await completed_environment()
    scan = await scan_context(ctx.store)
    wire = {"version": 1, "kind": "revision-test", "before": None}
    if fault == "version":
        wire["version"] = True
    elif fault == "scope":
        wire["kind"] = "another-kind"
    elif fault == "extra":
        wire["extra"] = "bad"
    else:
        wire["before"] = {"created_at": "2026-01-01T00:00:00", "task_id": "anchor"}
    await scan.put(revision_scan=wire)
    query = AsyncMock(wraps=ctx.store.list_tasks_before)
    monkeypatch.setattr(ctx.store, "list_tasks_before", query)
    with pytest.raises(ValueError):
        await reconciler(ctx.store, documents, vectors).run_page(scan)
    query.assert_not_awaited()


async def test_scan_budget_rejects_concurrent_context_growth_before_gc(
    monkeypatch,
) -> None:
    from comet_rag.tasks import TaskContextTooLarge

    ctx, documents, vectors, _ = await completed_environment()
    scan = await scan_context(ctx.store)
    await scan.put(unrelated="x" * 5000)
    query = AsyncMock(wraps=ctx.store.list_tasks_before)
    monkeypatch.setattr(ctx.store, "list_tasks_before", query)
    with pytest.raises(TaskContextTooLarge):
        await reconciler(ctx.store, documents, vectors).run_page(scan)
    query.assert_not_awaited()


async def test_parent_commit_then_cancel_without_handoff_is_precisely_recovered() -> (
    None
):
    from comet_rag.ports.document_store import RevisionStatus
    from comet_rag.services.indexing import IndexHandoff
    from comet_rag.services.revision_context import RevisionTaskStages, get_revision_key
    from tests.unit.services.test_revision_context import _context, _task_plan
    from tests.unit.services.test_revision_indexing import (
        FakeEmbedding,
        FaultyDocuments,
        FaultyVectors,
        stager,
    )

    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    documents, vectors, model = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    documents.fail_parent_on = 1
    plan = _task_plan(key)
    with pytest.raises(ConnectionError):
        await RevisionTaskStages(stager(documents, vectors, model)).prepare(ctx, plan)
    assert "revision_index" not in (await ctx.snapshot()).context
    assert len(await documents.get_parents("kb", [p.id for p in plan.parents])) == 1
    await ctx.store.transition(ctx.task_id, TaskStatus.CANCELLING)
    await ctx.store.transition(ctx.task_id, TaskStatus.CANCELLED)
    outcome = await reconciler(ctx.store, documents, vectors).reconcile(
        await ctx.snapshot()
    )
    assert outcome is ReconciliationDisposition.CLEANED
    assert await documents.get_parents("kb", [p.id for p in plan.parents]) == {}
    manifest = await documents.reserve_revision(
        IndexHandoff.from_plan(plan, task_id=ctx.task_id).draft
    )
    assert manifest.status is RevisionStatus.PURGED
    assert (await ctx.snapshot()).context["text"] == "abcdEFGHijkl"
    with pytest.raises(RevisionTaskRetained):
        await ctx.store.delete(
            ctx.task_id, force=True, guard=guard_revision_task_deletion
        )
    assert model.calls == vectors.calls == 0


async def test_reconciliation_rejects_generation_mismatch_before_vector_io(
    monkeypatch,
) -> None:
    from comet_rag.ports.document_store import RevisionConflict

    ctx, documents, vectors, _ = await completed_environment()
    source = await ctx.snapshot()
    source.context["revision_publication"]["generation"] = 999
    await ctx.store.update(ctx.task_id, context=source.context)
    delete = AsyncMock(wraps=vectors.delete)
    monkeypatch.setattr(vectors, "delete", delete)
    with pytest.raises(RevisionConflict, match="generation"):
        await reconciler(ctx.store, documents, vectors).reconcile(await ctx.snapshot())
    delete.assert_not_awaited()


async def test_reconciliation_uses_explicit_source_context_budget():
    from comet_rag.ports import DocumentResourceLimitExceeded

    ctx, documents, vectors, _ = await completed_environment()
    before = await ctx.snapshot()
    with pytest.raises(DocumentResourceLimitExceeded):
        await reconciler(ctx.store, documents, vectors, max_context_bytes=10).reconcile(
            before
        )
    assert await ctx.snapshot() == before
