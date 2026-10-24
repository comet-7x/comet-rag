from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.ports import VectorRecord
from comet_rag.ports.document_store import RevisionConflict, RevisionStatus
from comet_rag.services.indexing import IndexHandoff
from comet_rag.services.revision_context import RevisionTaskStages, get_revision_key
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.tasks import InMemoryTaskStore, StagePipeline, TaskContext, TaskStatus
from comet_rag.tasks.runner import TaskCancelled
from comet_rag.tasks.store import TaskLeaseLost
from tests.unit.services.test_revision_context import _context, _task_plan
from tests.unit.services.test_revision_gc import setup_gc
from tests.unit.services.test_revision_indexing import (
    FakeEmbedding,
    FaultyVectors,
    make_plan,
    stager,
)


async def test_late_remote_upsert_after_purge_is_not_found_by_regular_sweep() -> None:
    documents, vectors, old, _, outcome, collector = await setup_gc()
    submitted, commit = asyncio.Event(), asyncio.Event()

    async def remote_rpc() -> None:
        submitted.set()
        await commit.wait()
        await vectors.upsert(
            old.key.kb_id,
            [
                VectorRecord(
                    "late",
                    "late",
                    [1.0, 0.0],
                    {
                        "source_id": old.key.source_id,
                        "document_revision": old.key.revision_id,
                    },
                )
            ],
        )

    # 模拟请求已经离开进程；GC 的本地协程结束不能撤销远端执行。
    remote = asyncio.create_task(remote_rpc())
    try:
        await submitted.wait()
        await collector.collect_superseded(outcome)
        assert (await documents.reserve_revision(old)).status is RevisionStatus.PURGED
        commit.set()
        await remote
        condition = {"source_id": "source-1", "document_revision": "old"}
        assert await vectors.count("kb-1", filter=condition) == 1
        assert not (await collector.sweep()).completed
        assert await vectors.count("kb-1", filter=condition) == 1
        assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
            "source-1": "new"
        }
        # 精确重放有能力补偿；问题是 purged 已不在普通扫描里，身份不能随完成回执丢失。
        await collector.collect_superseded(outcome)
        assert await vectors.count("kb-1", filter=condition) == 0
    finally:
        commit.set()
        await remote


def stages_for(documents, vectors, model, **kwargs) -> RevisionTaskStages:
    return RevisionTaskStages(
        stager(documents, vectors, model),
        document_store=documents,
        garbage_collector=RevisionGarbageCollector(
            document_store=documents, vector_store=vectors
        ),
        **kwargs,
    )


async def prepared(*, superseded: bool = True):
    documents, vectors, model = (
        InMemoryDocumentStore(),
        FaultyVectors(),
        FakeEmbedding(),
    )
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    stages = stages_for(documents, vectors, model)
    await stages.prepare(ctx, _task_plan(key))
    await stages.stage(ctx, embedding_dim=2)
    if superseded:
        winner = make_plan("winner")
        await stager(documents, vectors, model).stage(winner, embedding_dim=2)
        await documents.activate_revision(winner.key)
    return ctx, documents, vectors, model, stages


async def receipt(ctx: TaskContext):
    return (await ctx.snapshot()).context["revision_publication"]


@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.parametrize(
    "write", ["intent-before", "intent-after", "result-before", "result-after"]
)
async def test_activation_persistence_failure_recovers_without_reembedding(
    superseded: bool,
    write: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, documents, vectors, model, stages = await prepared(superseded=superseded)
    put = ctx.put_bounded
    phase = (
        "activating"
        if write.startswith("intent")
        else ("cleanup_pending" if superseded else "active")
    )

    async def lost(limit, **values):
        if values.get("revision_publication", {}).get("phase") != phase:
            return await put(limit, **values)
        if write.endswith("after"):
            await put(limit, **values)
        raise ConnectionError("publication persistence lost")

    with monkeypatch.context() as patch:
        patch.setattr(ctx, "put_bounded", lost)
        with pytest.raises(ConnectionError):
            await stages.activate(ctx)
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    draft = IndexHandoff.from_plan(_task_plan(key), task_id=ctx.task_id).draft
    before = await documents.reserve_revision(draft)
    assert before.generation == 1
    assert before.status is (
        RevisionStatus.ACTIVE
        if not superseded and write.startswith("result")
        else RevisionStatus.READY
    )
    calls = model.calls, vectors.calls
    fresh = TaskContext(ctx.store, ctx.task_id, worker_id="cpu", attempts=1)
    outcome = await stages_for(documents, vectors, model).activate(fresh)
    assert outcome.superseded is superseded
    assert outcome.revision.generation == 1
    if before.activated_at:
        assert outcome.revision.activated_at == before.activated_at
    assert (model.calls, vectors.calls) == calls
    saved = json.loads(json.dumps(await receipt(fresh)))
    assert saved["key"]["revision_id"] == key.revision_id
    assert saved["phase"] == ("cleanup_pending" if superseded else "active")


@pytest.mark.parametrize(
    "fault",
    [
        "abandon-before",
        "abandon-after",
        "delete-after",
        "purge-after",
        "ack-before",
        "ack-after",
    ],
)
async def test_cleanup_failure_recovers_from_durable_intent_without_reactivation(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, documents, vectors, model, stages = await prepared()
    outcome = await stages.activate(ctx)
    abandon, delete, purge, put = (
        documents.abandon_revision,
        vectors.delete,
        documents.purge_inactive_revision,
        ctx.put_bounded,
    )

    async def bad_abandon(key):
        if fault == "abandon-after":
            await abandon(key)
        raise ConnectionError("abandon failed")

    async def bad_delete(kb, **kwargs):
        await delete(kb, **kwargs)
        raise ConnectionError("delete failed")

    async def bad_purge(key):
        await purge(key)
        raise ConnectionError("purge response lost")

    async def bad_ack(limit, **values):
        if fault == "ack-after":
            await put(limit, **values)
        raise ConnectionError("ack failed")

    with monkeypatch.context() as patch:
        if fault.startswith("abandon"):
            patch.setattr(documents, "abandon_revision", bad_abandon)
        elif fault.startswith("delete"):
            patch.setattr(vectors, "delete", bad_delete)
        elif fault.startswith("purge"):
            patch.setattr(documents, "purge_inactive_revision", bad_purge)
        else:
            patch.setattr(ctx, "put_bounded", bad_ack)
        with pytest.raises(ConnectionError):
            await stages.cleanup(ctx)
    assert (await receipt(ctx))["phase"] == (
        "cleanup_complete" if fault == "ack-after" else "cleanup_pending"
    )
    calls = model.calls, vectors.calls
    fresh = TaskContext(ctx.store, ctx.task_id, worker_id="cpu", attempts=1)
    resumed = stages_for(documents, vectors, model)
    activate = AsyncMock(
        side_effect=AssertionError("must not reactivate cleanup intent")
    )
    monkeypatch.setattr(documents, "activate_revision", activate)
    assert (await resumed.activate(fresh)).superseded
    await resumed.cleanup(fresh)
    activate.assert_not_awaited()
    assert (await receipt(ctx))["phase"] == "cleanup_complete"
    manifest = await documents.reserve_revision(
        IndexHandoff.from_plan(
            _task_plan(outcome.revision.key), task_id=ctx.task_id
        ).draft
    )
    assert manifest.status is RevisionStatus.PURGED and manifest.generation == 1
    assert (
        await vectors.count(
            "kb", filter={"document_revision": outcome.revision.key.revision_id}
        )
        == 0
    )
    assert await vectors.count("kb", filter={"document_revision": "winner"}) == 4
    assert (model.calls, vectors.calls) == calls
    with pytest.raises(RevisionConflict, match="禁止重写"):
        await resumed.stage(fresh, embedding_dim=2)


async def test_completed_task_intent_can_compensate_upsert_after_gc() -> None:
    ctx, documents, vectors, model, stages = await prepared()
    outcome = await stages.activate(ctx)
    await stages.cleanup(ctx)
    saved = await receipt(ctx)
    assert saved["phase"] == "cleanup_complete"
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
    assert not (
        await RevisionGarbageCollector(
            document_store=documents, vector_store=vectors
        ).sweep()
    ).completed
    assert (
        await vectors.count(
            "kb", filter={"document_revision": outcome.revision.key.revision_id}
        )
        == 1
    )
    # 重放完成回执仍删向量，不能因为 purged/complete 直接返回。
    await stages_for(documents, vectors, model).cleanup(
        TaskContext(ctx.store, ctx.task_id)
    )
    assert (
        await vectors.count(
            "kb", filter={"document_revision": outcome.revision.key.revision_id}
        )
        == 0
    )
    assert await receipt(ctx) == saved
    assert await documents.get_active_revisions("kb", ["source"]) == {
        "source": "winner"
    }
    assert await vectors.count("kb", filter={"document_revision": "winner"}) == 4


async def test_active_result_is_revalidated_after_newer_activation() -> None:
    ctx, documents, vectors, model, stages = await prepared(superseded=False)
    first = await stages.activate(ctx)
    assert (await receipt(ctx))["phase"] == "active"
    await stages.cleanup(ctx)
    assert await vectors.count("kb") == 4
    newer = make_plan("newer")
    await stager(documents, vectors, model).stage(newer, embedding_dim=2)
    await documents.activate_revision(newer.key)
    outcome = await stages_for(documents, vectors, model).activate(ctx)
    assert (
        outcome.superseded
        and outcome.revision.activated_at == first.revision.activated_at
    )
    assert (await receipt(ctx))["phase"] == "cleanup_pending"
    await stages.cleanup(ctx)
    assert await documents.get_active_revisions("kb", ["source"]) == {"source": "newer"}
    assert await vectors.count("kb") == 4


@pytest.mark.parametrize(
    "fault",
    [
        "key",
        "digest",
        "task",
        "generation-bool",
        "generation-mismatch",
        "version",
        "phase",
        "extra",
    ],
)
async def test_malformed_receipt_fails_before_destructive_io(
    fault, monkeypatch
) -> None:
    ctx, documents, vectors, _, stages = await prepared()
    await stages.activate(ctx)
    value = await receipt(ctx)
    if fault == "key":
        value["key"]["revision_id"] = "winner"
    elif fault == "digest":
        value["plan_sha256"] = "0" * 64
    elif fault == "task":
        value["task_id"] = "another-task"
    elif fault.startswith("generation"):
        value["generation"] = True if fault.endswith("bool") else 999
    elif fault == "version":
        value["version"] = True
    elif fault == "extra":
        value["unsafe"] = True
    else:
        value["phase"] = "unknown"
    await ctx.put(revision_publication=value)
    delete = AsyncMock(wraps=vectors.delete)
    monkeypatch.setattr(vectors, "delete", delete)
    with pytest.raises((ValueError, TypeError, RevisionConflict)):
        await stages.cleanup(ctx)
    delete.assert_not_awaited()


@pytest.mark.parametrize("fault", ["cancel", "lease"])
@pytest.mark.parametrize("boundary", ["abandon", "delete", "purge"])
async def test_lost_ownership_after_abandon_keeps_pending_intent(
    fault, boundary, monkeypatch
) -> None:
    ctx, documents, vectors, model, stages = await prepared()
    await stages.activate(ctx)
    operation = {
        "abandon": documents.abandon_revision,
        "delete": vectors.delete,
        "purge": documents.purge_inactive_revision,
    }[boundary]

    async def lose(*args, **kwargs):
        result = await operation(*args, **kwargs)
        await ctx.store.transition(
            ctx.task_id,
            TaskStatus.CANCELLING if fault == "cancel" else TaskStatus.PENDING,
        )
        return result

    with monkeypatch.context() as patch:
        delete = AsyncMock(wraps=vectors.delete)
        patch.setattr(vectors, "delete", delete)
        if boundary == "delete":
            delete.side_effect = lose
        elif boundary == "abandon":
            patch.setattr(documents, "abandon_revision", lose)
        else:
            patch.setattr(documents, "purge_inactive_revision", lose)
        with pytest.raises(TaskCancelled if fault == "cancel" else TaskLeaseLost):
            await stages.cleanup(ctx)
        assert delete.await_count == (0 if boundary == "abandon" else 1)
    assert (await receipt(ctx))["phase"] == "cleanup_pending"
    assert await documents.get_active_revisions("kb", ["source"]) == {
        "source": "winner"
    }


async def test_failed_gc_ack_resumes_only_cleanup_stage(monkeypatch) -> None:
    ctx, documents, vectors, model, stages = await prepared()
    calls = []
    flow = StagePipeline()

    @flow.stage("activation")
    async def activate(context):
        calls.append("activation")
        await stages.activate(context)

    @flow.stage("cleanup")
    async def cleanup(context):
        calls.append("cleanup")
        await stages.cleanup(context)

    put = ctx.put_bounded

    async def lost(limit, **values):
        if values.get("revision_publication", {}).get("phase") == "cleanup_complete":
            raise ConnectionError("GC ack lost")
        await put(limit, **values)

    with monkeypatch.context() as patch:
        patch.setattr(ctx, "put_bounded", lost)
        with pytest.raises(ConnectionError):
            await flow(ctx)
    await ctx.store.transition(ctx.task_id, TaskStatus.FAILED)
    await ctx.store.transition(ctx.task_id, TaskStatus.PENDING)
    await ctx.store.transition(
        ctx.task_id, TaskStatus.RUNNING, worker_id="retry", attempts=2
    )
    assert (await ctx.snapshot()).resume_stage == "cleanup"
    stages = stages_for(documents, vectors, model)
    await flow(TaskContext(ctx.store, ctx.task_id, worker_id="retry", attempts=2))
    assert calls == ["activation", "cleanup", "cleanup"]
    assert (await receipt(ctx))["phase"] == "cleanup_complete"


async def test_cleanup_revalidates_active_receipt_and_registers_gc_before_delete(
    monkeypatch,
) -> None:
    ctx, documents, vectors, model, stages = await prepared(superseded=False)
    await stages.activate(ctx)
    newer = make_plan("newer")
    await stager(documents, vectors, model).stage(newer, embedding_dim=2)
    await documents.activate_revision(newer.key)
    delete = vectors.delete

    async def check_intent(kb, **kwargs):
        assert (await receipt(ctx))["phase"] == "cleanup_pending"
        return await delete(kb, **kwargs)

    monkeypatch.setattr(vectors, "delete", check_intent)
    await stages.cleanup(ctx)
    assert (await receipt(ctx))["phase"] == "cleanup_complete"
    assert await documents.get_active_revisions("kb", ["source"]) == {"source": "newer"}
    assert await vectors.count("kb") == 4


async def test_publication_budget_is_checked_before_activation(monkeypatch) -> None:
    from comet_rag.ports import DocumentResourceLimitExceeded

    ctx, documents, vectors, model, _ = await prepared(superseded=False)
    size = len(
        json.dumps(
            (await ctx.snapshot()).context,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    )
    stages = stages_for(documents, vectors, model, max_context_bytes=size + 20)
    activate = AsyncMock(wraps=documents.activate_revision)
    monkeypatch.setattr(documents, "activate_revision", activate)
    with pytest.raises(DocumentResourceLimitExceeded):
        await stages.activate(ctx)
    activate.assert_not_awaited()
    assert "revision_publication" not in (await ctx.snapshot()).context
    assert await documents.get_active_revisions("kb", ["source"]) == {"source": None}


async def test_premature_activation_does_not_block_indexing() -> None:
    from comet_rag.ports.document_store import RevisionNotReady

    ctx, documents, vectors, model = (
        await _context(InMemoryTaskStore()),
        InMemoryDocumentStore(),
        FaultyVectors(),
        FakeEmbedding(),
    )
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    stages = stages_for(documents, vectors, model)
    await stages.prepare(ctx, _task_plan(key))
    with pytest.raises(RevisionNotReady):
        await stages.activate(ctx)
    assert "revision_publication" not in (await ctx.snapshot()).context
    assert (await stages.stage(ctx, embedding_dim=2)).status is RevisionStatus.READY


async def test_no_cleanup_intent_and_missing_collector_are_rejected() -> None:
    ctx, documents, vectors, model, stages = await prepared()
    with pytest.raises(RevisionConflict, match="激活结果"):
        await stages.cleanup(ctx)
    await stages.activate(ctx)
    without_gc = RevisionTaskStages(
        stager(documents, vectors, model), document_store=documents
    )
    with pytest.raises(ValueError, match="显式注入"):
        await without_gc.cleanup(ctx)


async def test_forged_cleanup_intent_cannot_delete_current_active(monkeypatch) -> None:
    from comet_rag.ports.document_store import ActiveRevisionDeletion

    ctx, documents, vectors, _, stages = await prepared(superseded=False)
    await stages.activate(ctx)
    value = await receipt(ctx)
    value["phase"] = "cleanup_pending"
    await ctx.put(revision_publication=value)
    delete = AsyncMock(wraps=vectors.delete)
    monkeypatch.setattr(vectors, "delete", delete)
    with pytest.raises(ActiveRevisionDeletion):
        await stages.cleanup(ctx)
    delete.assert_not_awaited()
    assert await vectors.count("kb") == 4
