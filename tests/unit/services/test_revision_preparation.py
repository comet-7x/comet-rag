from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from comet_rag.ports.document_store import RevisionConflict, RevisionStatus
from comet_rag.services.revision_context import (
    RevisionTaskStages,
    get_revision_key,
    load_preparation,
    load_task_handoff,
)
from comet_rag.tasks import InMemoryTaskStore, TaskContext
from tests.unit.services.test_revision_context import _context, _task_plan
from tests.unit.services.test_revision_indexing import (
    FakeEmbedding,
    FaultyDocuments,
    FaultyVectors,
    stager,
)


async def environment():
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    documents, vectors, model = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    return ctx, _task_plan(key), documents, vectors, model


@pytest.mark.parametrize("committed", [False, True])
async def test_preparation_ack_loss_precedes_all_document_io(committed, monkeypatch):
    ctx, plan, documents, vectors, model = await environment()
    put = ctx.put_bounded
    reserve = AsyncMock(wraps=documents.reserve_revision)
    monkeypatch.setattr(documents, "reserve_revision", reserve)

    async def lose(limit, **values):
        assert "revision_prepare" in values and "revision_index" not in values
        if committed:
            await put(limit, **values)
        raise ConnectionError("preparation acknowledgment lost")

    with monkeypatch.context() as patch:
        patch.setattr(ctx, "put_bounded", lose)
        with pytest.raises(ConnectionError):
            await RevisionTaskStages(stager(documents, vectors, model)).prepare(
                ctx, plan
            )
    reserve.assert_not_awaited()
    saved = await ctx.snapshot()
    assert saved.context["text"] == "abcdEFGHijkl"
    assert (load_preparation(saved.context, ctx.task_id) is not None) == committed
    fresh = TaskContext(ctx.store, ctx.task_id, worker_id="cpu", attempts=1)
    handoff = await RevisionTaskStages(stager(documents, vectors, model)).prepare(
        fresh, plan
    )
    manifest = await documents.reserve_revision(handoff.draft)
    assert manifest.generation == 1 and manifest.status is RevisionStatus.STAGING
    assert (await fresh.snapshot()).context["revision_prepare"] is None
    assert model.calls == vectors.calls == 0


@pytest.mark.parametrize("operation", ["reserve_revision", "put_parents"])
@pytest.mark.parametrize("committed", [False, True])
async def test_document_failure_always_has_durable_plan_and_retains_input(
    operation, committed, monkeypatch
):
    ctx, plan, documents, vectors, model = await environment()
    original = getattr(documents, operation)

    async def lose(*args, **kwargs):
        saved = await ctx.snapshot()
        preparation = load_preparation(saved.context, ctx.task_id)
        assert preparation is not None and preparation.plan_sha256 == plan.plan_sha256
        assert preparation.parent_count == 2 and preparation.child_count == 4
        assert saved.context["text"] == "abcdEFGHijkl"
        assert "children" not in saved.context["revision_prepare"]
        if committed:
            await original(*args, **kwargs)
        raise ConnectionError("document acknowledgment lost")

    with monkeypatch.context() as patch:
        patch.setattr(documents, operation, lose)
        with pytest.raises(ConnectionError):
            await RevisionTaskStages(stager(documents, vectors, model)).prepare(
                ctx, plan
            )
    saved = await ctx.snapshot()
    assert "revision_index" not in saved.context
    assert saved.context["text"] == "abcdEFGHijkl"
    await RevisionTaskStages(stager(documents, vectors, model)).prepare(ctx, plan)
    handoff = load_task_handoff((await ctx.snapshot()).context, ctx.task_id)
    assert len(await documents.get_parents("kb", [p.id for p in plan.parents])) == 2
    assert (await documents.reserve_revision(handoff.draft)).generation == 1
    assert model.calls == vectors.calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "other"),
        ("plan_sha256", "0" * 64),
        ("parent_count", 999),
        ("child_count", True),
    ],
)
async def test_preparation_corruption_rejects_retry_before_external_io(
    field, value, monkeypatch
):
    ctx, plan, documents, vectors, model = await environment()
    documents.fail_parent_on = 1
    with pytest.raises(ConnectionError):
        await RevisionTaskStages(stager(documents, vectors, model)).prepare(ctx, plan)
    task = await ctx.snapshot()
    task.context["revision_prepare"]["draft"][field] = value
    await ctx.store.update(ctx.task_id, context=task.context)
    reserve = AsyncMock(wraps=documents.reserve_revision)
    monkeypatch.setattr(documents, "reserve_revision", reserve)
    with pytest.raises((RevisionConflict, TypeError)):
        await RevisionTaskStages(stager(documents, vectors, model)).prepare(ctx, plan)
    reserve.assert_not_awaited()


async def test_handoff_and_preparation_cannot_disagree():
    ctx, plan, documents, vectors, model = await environment()
    documents.fail_parent_on = 1
    with pytest.raises(ConnectionError):
        await RevisionTaskStages(stager(documents, vectors, model)).prepare(ctx, plan)
    preparation = deepcopy((await ctx.snapshot()).context["revision_prepare"])
    documents.fail_parent_on = 0
    await RevisionTaskStages(stager(documents, vectors, model)).prepare(ctx, plan)
    task = await ctx.snapshot()
    preparation["draft"]["plan_sha256"] = "0" * 64
    task.context["revision_prepare"] = preparation
    with pytest.raises(RevisionConflict, match="准备意图"):
        load_task_handoff(task.context, ctx.task_id)


@pytest.mark.parametrize("corruption", ["owner", "version"])
async def test_cancelled_preparation_is_validated_before_any_cleanup(
    corruption, monkeypatch
):
    from comet_rag.tasks import TaskStatus
    from tests.unit.services.test_revision_reconciliation import reconciler

    ctx, plan, documents, vectors, model = await environment()
    documents.fail_parent_on = 1
    with pytest.raises(ConnectionError):
        await RevisionTaskStages(stager(documents, vectors, model)).prepare(ctx, plan)
    task = await ctx.snapshot()
    if corruption == "owner":
        task.context["revision_prepare"]["draft"]["task_id"] = "someone-else"
    else:
        task.context["revision_prepare"]["version"] = True
    await ctx.store.update(ctx.task_id, context=task.context)
    await ctx.store.transition(ctx.task_id, TaskStatus.CANCELLING)
    await ctx.store.transition(ctx.task_id, TaskStatus.CANCELLED)
    reserve = AsyncMock(wraps=documents.reserve_revision)
    monkeypatch.setattr(documents, "reserve_revision", reserve)
    with pytest.raises((RevisionConflict, ValueError)):
        await reconciler(ctx.store, documents, vectors).reconcile(await ctx.snapshot())
    reserve.assert_not_awaited()
