from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.ports import DocumentResourceLimitExceeded
from comet_rag.ports.document_store import (
    RevisionConflict,
    RevisionNotReady,
    RevisionStatus,
)
from comet_rag.services.revision_activation import RevisionActivator
from comet_rag.services.revision_context import RevisionTaskStages, get_revision_key
from comet_rag.tasks import InMemoryTaskStore
from comet_rag.tasks.runner import TaskCancelled
from comet_rag.tasks.store import TaskLeaseLost
from tests.contracts.document_store import make_draft, make_ready
from tests.unit.services.test_revision_context import _context, _task_plan
from tests.unit.services.test_revision_indexing import (
    FakeEmbedding,
    FaultyVectors,
    stager,
)


async def test_failed_activation_preserves_old_pointer_and_ready_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = InMemoryDocumentStore()
    old, _ = await make_ready(documents, make_draft("old"))
    await documents.activate_revision(old.key)
    new, _ = await make_ready(documents, make_draft("new"))
    activate = documents.activate_revision
    failure = AsyncMock(side_effect=ConnectionError("activation unavailable"))
    monkeypatch.setattr(documents, "activate_revision", failure)
    with pytest.raises(ConnectionError, match="activation unavailable"):
        await RevisionActivator(documents).activate(new)
    assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
        "source-1": "old"
    }
    assert (await documents.reserve_revision(old)).status is RevisionStatus.ACTIVE
    assert (await documents.reserve_revision(new)).status is RevisionStatus.READY
    assert await documents.list_cleanup_candidates(limit=10) == []
    monkeypatch.setattr(documents, "activate_revision", activate)
    result = await RevisionActivator(documents).activate(new)
    assert result.revision.generation == 2
    assert not result.superseded and not result.already_active


@pytest.mark.parametrize("intervening_newer", [False, True])
async def test_committed_activation_response_loss_survives_new_service_and_newer_writer(
    intervening_newer: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = InMemoryDocumentStore()
    old, _ = await make_ready(documents, make_draft("old"))
    await documents.activate_revision(old.key)
    target, _ = await make_ready(documents, make_draft("target"))
    activate = documents.activate_revision

    async def lose_response(key):
        await activate(key)
        raise ConnectionError("response lost after activation commit")

    monkeypatch.setattr(documents, "activate_revision", lose_response)
    with pytest.raises(ConnectionError, match="after activation commit"):
        await RevisionActivator(documents).activate(target)
    committed = await documents.reserve_revision(target)
    assert committed.status is RevisionStatus.ACTIVE
    assert committed.generation == 2
    if intervening_newer:
        newer, _ = await make_ready(documents, make_draft("newer"))
        await activate(newer.key)
    monkeypatch.setattr(documents, "activate_revision", activate)
    outcome = await RevisionActivator(documents).activate(target)
    assert outcome.superseded is intervening_newer
    assert outcome.already_active is not intervening_newer
    assert outcome.revision.generation == 2
    assert outcome.revision.activated_at == committed.activated_at
    assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
        "source-1": "newer" if intervening_newer else "target"
    }
    following = await documents.reserve_revision(make_draft("following"))
    assert following.generation == (4 if intervening_newer else 3)


async def test_stale_ready_is_superseded_repeatably_without_deleting_any_revision() -> (
    None
):
    documents = InMemoryDocumentStore()
    old, _ = await make_ready(documents, make_draft("old"))
    new, _ = await make_ready(documents, make_draft("new"))
    await documents.activate_revision(new.key)
    for _ in range(2):
        outcome = await RevisionActivator(documents).activate(old)
        assert outcome.superseded and not outcome.already_active
        assert outcome.revision.generation == 1
    assert (await documents.reserve_revision(old)).status is RevisionStatus.READY
    assert (await documents.reserve_revision(new)).status is RevisionStatus.ACTIVE
    assert set(
        await documents.get_parents("kb-1", ["old-parent-0", "new-parent-0"])
    ) == {"old-parent-0", "new-parent-0"}
    assert await documents.list_cleanup_candidates(limit=10) == []


@pytest.mark.parametrize("initially_active", [False, True])
async def test_new_writer_between_manifest_read_and_activation_cannot_be_overwritten(
    initially_active: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = InMemoryDocumentStore()
    old, _ = await make_ready(documents, make_draft("old"))
    if initially_active:
        await documents.activate_revision(old.key)
    new, _ = await make_ready(documents, make_draft("new"))
    activate = documents.activate_revision

    async def race(key):
        await activate(new.key)
        return await activate(key)

    monkeypatch.setattr(documents, "activate_revision", race)
    outcome = await RevisionActivator(documents).activate(old)
    assert outcome.superseded
    assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
        "source-1": "new"
    }


@pytest.mark.parametrize("status", ["staging", "abandoned", "purged"])
async def test_unpublished_revision_is_not_reported_as_success(status: str) -> None:
    documents = InMemoryDocumentStore()
    draft = make_draft()
    await documents.reserve_revision(draft)
    if status != "staging":
        await documents.abandon_revision(draft.key)
    if status == "purged":
        await documents.purge_inactive_revision(draft.key)
    with pytest.raises(RevisionNotReady):
        await RevisionActivator(documents).activate(draft)
    assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
        "source-1": None
    }


@pytest.mark.parametrize("field", ["plan_sha256", "child_count", "task_id"])
async def test_manifest_identity_is_checked_even_after_activation(field: str) -> None:
    documents = InMemoryDocumentStore()
    draft, _ = await make_ready(documents, make_draft())
    await documents.activate_revision(draft.key)
    changed = replace(
        draft,
        **{
            field: {"plan_sha256": "b" * 64, "child_count": 3, "task_id": "other"}[
                field
            ]
        },
    )
    with pytest.raises(RevisionConflict):
        await RevisionActivator(documents).activate(changed)


@pytest.mark.parametrize("stop_on", [1, 2])
@pytest.mark.parametrize(
    "error", [TaskCancelled, TaskLeaseLost, asyncio.CancelledError]
)
async def test_checkpoint_failure_prevents_activation(
    stop_on: int, error: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = InMemoryDocumentStore()
    draft, _ = await make_ready(documents, make_draft())
    activate = AsyncMock(wraps=documents.activate_revision)
    monkeypatch.setattr(documents, "activate_revision", activate)
    calls = 0

    async def checkpoint():
        nonlocal calls
        calls += 1
        if calls == stop_on:
            raise error("ownership lost")

    with pytest.raises(error):
        await RevisionActivator(documents).activate(draft, checkpoint=checkpoint)
    activate.assert_not_awaited()
    assert (await documents.reserve_revision(draft)).status is RevisionStatus.READY


async def test_activation_task_retry_after_report_failure_does_not_reembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, embedding = (
        InMemoryDocumentStore(),
        FaultyVectors(),
        FakeEmbedding(),
    )
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    plan = _task_plan(key)
    stages = RevisionTaskStages(
        stager(documents, vectors, embedding), document_store=documents
    )
    await stages.prepare(ctx, plan)
    await stages.stage(ctx, embedding_dim=2)
    before = embedding.calls, vectors.calls
    report = ctx.report
    monkeypatch.setattr(
        ctx, "report", AsyncMock(side_effect=ConnectionError("report lost"))
    )
    with pytest.raises(ConnectionError, match="report lost"):
        await stages.activate(ctx)
    monkeypatch.setattr(ctx, "report", report)
    outcome = await stages.activate(ctx)
    assert outcome.already_active and not outcome.superseded
    assert outcome.revision.generation == 1
    assert (embedding.calls, vectors.calls) == before
    assert await documents.get_active_revisions("kb", ["source"]) == {
        "source": key.revision_id
    }
    assert (await ctx.snapshot()).context["revision_index"]["draft"]["key"][
        "revision_id"
    ] == key.revision_id


async def test_task_activation_requires_explicit_dependency() -> None:
    ctx = await _context(InMemoryTaskStore())
    stages = RevisionTaskStages(
        stager(InMemoryDocumentStore(), FaultyVectors(), FakeEmbedding())
    )
    with pytest.raises(ValueError, match="显式注入"):
        await stages.activate(ctx)


async def test_not_ready_during_activation_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = InMemoryDocumentStore()
    draft, _ = await make_ready(documents, make_draft())
    monkeypatch.setattr(
        documents,
        "activate_revision",
        AsyncMock(side_effect=RevisionNotReady(draft.key, "broken invariant")),
    )
    with pytest.raises(RevisionNotReady, match="broken invariant"):
        await RevisionActivator(documents).activate(draft)


async def test_cancel_after_commit_keeps_published_revision_and_retry_recognizes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = InMemoryDocumentStore()
    draft, _ = await make_ready(documents, make_draft())
    activate = documents.activate_revision

    async def interrupted(key):
        await activate(key)
        raise asyncio.CancelledError

    monkeypatch.setattr(documents, "activate_revision", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await RevisionActivator(documents).activate(draft)
    monkeypatch.setattr(documents, "activate_revision", activate)
    outcome = await RevisionActivator(documents).activate(draft)
    assert outcome.already_active and not outcome.superseded
    assert outcome.revision.status is RevisionStatus.ACTIVE


async def test_purged_former_active_is_superseded_but_never_recreated() -> None:
    documents = InMemoryDocumentStore()
    old, _ = await make_ready(documents, make_draft("old"))
    new, _ = await make_ready(documents, make_draft("new"))
    await documents.activate_revision(old.key)
    await documents.activate_revision(new.key)
    await documents.purge_inactive_revision(old.key)
    outcome = await RevisionActivator(documents).activate(old)
    assert outcome.superseded
    assert outcome.revision.status is RevisionStatus.PURGED
    assert await documents.get_parents("kb-1", ["old-parent-0"]) == {}


@pytest.mark.parametrize("fault", ["task-id", "revision-key", "context-budget"])
async def test_task_activation_checks_persisted_identity_before_calling_activator(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = InMemoryDocumentStore()
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    stages = RevisionTaskStages(
        stager(documents, FaultyVectors(), FakeEmbedding()),
        document_store=documents,
        max_context_bytes=10000,
    )
    await stages.prepare(ctx, _task_plan(key))
    task = await ctx.snapshot()
    if fault == "task-id":
        task.context["revision_index"]["draft"]["task_id"] = "another-task"
    elif fault == "revision-key":
        task.context["revision_key"]["source_id"] = "another-source"
    else:
        task.context["extra"] = "x" * 10000
    await ctx.put(**task.context)
    activate = AsyncMock()
    monkeypatch.setattr(documents, "activate_revision", activate)
    with pytest.raises((RevisionConflict, DocumentResourceLimitExceeded)):
        await stages.activate(ctx)
    activate.assert_not_awaited()
