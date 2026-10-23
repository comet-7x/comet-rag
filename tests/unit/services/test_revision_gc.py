from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import VectorRecord
from comet_rag.ports.document_store import (
    ActiveRevisionDeletion,
    RevisionNotReady,
    RevisionStatus,
)
from comet_rag.services.revision_activation import ActivationOutcome, RevisionActivator
from comet_rag.services.revision_gc import RevisionGarbageCollector
from tests.contracts.document_store import make_draft, make_ready


async def setup_gc():
    documents, vectors = InMemoryDocumentStore(), AsyncInMemoryVectorStore()
    old, _ = await make_ready(documents, make_draft("old"))
    new, _ = await make_ready(documents, make_draft("new"))
    await documents.activate_revision(new.key)
    outcome = await RevisionActivator(documents).activate(old)
    await vectors.ensure_collection("kb-1", dim=2)
    await vectors.upsert(
        "kb-1",
        [
            VectorRecord(
                "old-1",
                "old",
                [1.0, 0.0],
                {"source_id": "source-1", "document_revision": "old"},
            ),
            VectorRecord(
                "old-2",
                "old",
                [1.0, 0.0],
                {"source_id": "source-1", "document_revision": "old"},
            ),
            VectorRecord(
                "new",
                "new",
                [1.0, 0.0],
                {"source_id": "source-1", "document_revision": "new"},
            ),
            VectorRecord(
                "other-source",
                "other",
                [1.0, 0.0],
                {"source_id": "other", "document_revision": "old"},
            ),
            VectorRecord("legacy", "legacy", [1.0, 0.0], {"source_id": "source-1"}),
        ],
    )
    await vectors.ensure_collection("other-kb", dim=2)
    await vectors.upsert(
        "other-kb",
        [
            VectorRecord(
                "foreign",
                "foreign",
                [1.0, 0.0],
                {"source_id": "source-1", "document_revision": "old"},
            )
        ],
    )
    collector = RevisionGarbageCollector(document_store=documents, vector_store=vectors)
    return documents, vectors, old, new, outcome, collector


async def assert_survivors(documents, vectors):
    assert {row["id"] for row in vectors.snapshot()["kb-1"]} == {
        "new",
        "other-source",
        "legacy",
    }
    assert {row["id"] for row in vectors.snapshot()["other-kb"]} == {"foreign"}
    assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
        "source-1": "new"
    }
    assert set(
        await documents.get_parents("kb-1", ["old-parent-0", "new-parent-0"])
    ) == {"new-parent-0"}


async def test_superseded_gc_is_exact_idempotent_and_keeps_manifest_tombstone() -> None:
    documents, vectors, old, _, outcome, gc = await setup_gc()
    for _ in range(2):
        await gc.collect_superseded(outcome)
        await assert_survivors(documents, vectors)
        row = await documents.reserve_revision(old)
        assert row.status is RevisionStatus.PURGED and row.generation == 1
    assert await documents.list_cleanup_candidates(limit=32) == []


@pytest.mark.parametrize(
    "fault",
    [
        "partial-delete",
        "silent-partial",
        "delete-response",
        "count",
        "purge-before",
        "purge-response",
        "abandon-response",
    ],
)
async def test_partial_failure_keeps_retry_path_and_never_removes_active(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, old, _, outcome, gc = await setup_gc()
    delete = vectors.delete
    purge, abandon = documents.purge_inactive_revision, documents.abandon_revision

    async def bad_delete(kb_id, *, filter):
        if fault in ("partial-delete", "silent-partial"):
            await delete(kb_id, ids=["old-1"], filter=filter)
            if fault == "silent-partial":
                return 1
        else:
            await delete(kb_id, filter=filter)
        raise ConnectionError("delete response lost")

    async def bad_purge(key):
        if fault == "purge-response":
            await purge(key)
        raise ConnectionError("purge response lost")

    async def bad_abandon(key):
        await abandon(key)
        raise ConnectionError("abandon response lost")

    with monkeypatch.context() as patch:
        if fault in ("partial-delete", "silent-partial", "delete-response"):
            patch.setattr(vectors, "delete", bad_delete)
        elif fault == "count":
            patch.setattr(
                vectors,
                "count",
                AsyncMock(side_effect=ConnectionError("count unavailable")),
            )
        elif fault.startswith("purge"):
            patch.setattr(documents, "purge_inactive_revision", bad_purge)
        else:
            patch.setattr(documents, "abandon_revision", bad_abandon)
        with pytest.raises((ConnectionError, RuntimeError)):
            await gc.collect_superseded(outcome)
    state = await documents.reserve_revision(old)
    assert state.status is (
        RevisionStatus.PURGED if fault == "purge-response" else RevisionStatus.ABANDONED
    )
    assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
        "source-1": "new"
    }
    if fault != "purge-response":
        assert old.key in await documents.list_cleanup_candidates(limit=32)
        assert "old-parent-0" in await documents.get_parents("kb-1", ["old-parent-0"])
        # 新实例无需原 outcome，仅凭持久化候选即可完成失败后的恢复。
        report = await RevisionGarbageCollector(
            document_store=documents, vector_store=vectors
        ).sweep()
        assert report.completed == (old.key,) and not report.failed
    else:
        await gc.collect_superseded(outcome)
    await assert_survivors(documents, vectors)


async def test_active_guard_runs_before_vector_delete_even_with_bad_candidate_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, _, new, _, gc = await setup_gc()
    monkeypatch.setattr(
        documents, "list_cleanup_candidates", AsyncMock(return_value=[new.key])
    )
    delete = AsyncMock(wraps=vectors.delete)
    monkeypatch.setattr(vectors, "delete", delete)
    report = await gc.sweep()
    assert report.failed == (new.key,) and not report.completed
    delete.assert_not_awaited()
    assert (await documents.reserve_revision(new)).status is RevisionStatus.ACTIVE
    assert {row["id"] for row in vectors.snapshot()["kb-1"]} == {
        "old-1",
        "old-2",
        "new",
        "other-source",
        "legacy",
    }


async def test_forged_ready_snapshot_cannot_delete_current_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, _, new, _, gc = await setup_gc()
    active = await documents.reserve_revision(new)
    delete = AsyncMock(wraps=vectors.delete)
    monkeypatch.setattr(vectors, "delete", delete)
    with pytest.raises(ActiveRevisionDeletion):
        await gc.collect_superseded(
            ActivationOutcome(
                replace(active, status=RevisionStatus.READY), superseded=True
            )
        )
    delete.assert_not_awaited()


async def test_sweep_is_bounded_and_one_failure_does_not_hide_other_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, old, _, _, gc = await setup_gc()
    await documents.abandon_revision(old.key)
    other, _ = await make_ready(documents, make_draft("other"))
    await documents.abandon_revision(other.key)
    third, _ = await make_ready(documents, make_draft("third"))
    await documents.abandon_revision(third.key)
    original = documents.purge_inactive_revision
    log = Mock()
    monkeypatch.setattr("comet_rag.services.revision_gc.logger.exception", log)

    async def fail_one(key):
        if key == old.key:
            raise ConnectionError("parent transaction unavailable")
        await original(key)

    monkeypatch.setattr(documents, "purge_inactive_revision", fail_one)
    report = await gc.sweep(limit=2)
    assert report.completed == (other.key,) and report.failed == (old.key,)
    assert (await documents.reserve_revision(third)).status is RevisionStatus.ABANDONED
    assert await documents.list_cleanup_candidates(limit=32) == [old.key, third.key]
    log.assert_called_once()


@pytest.mark.parametrize(
    "cancel_stage", ["abandon_revision", "delete", "count", "purge_inactive_revision"]
)
async def test_cancellation_is_not_swallowed_and_remaining_work_can_be_swept(
    cancel_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, old, _, _, gc = await setup_gc()
    await documents.abandon_revision(old.key)
    owner = vectors if cancel_stage in ("delete", "count") else documents
    with monkeypatch.context() as patch:
        patch.setattr(
            owner, cancel_stage, AsyncMock(side_effect=asyncio.CancelledError)
        )
        with pytest.raises(asyncio.CancelledError):
            await gc.sweep()
    assert old.key in await documents.list_cleanup_candidates(limit=32)
    assert (await gc.sweep()).completed == (old.key,)
    await assert_survivors(documents, vectors)


async def test_missing_collection_is_empty_but_connection_errors_are_not() -> None:
    documents, vectors, old, _, outcome, gc = await setup_gc()
    await vectors.drop_collection("kb-1")
    await gc.collect_superseded(outcome)
    assert (await documents.reserve_revision(old)).status is RevisionStatus.PURGED


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
async def test_invalid_limit_is_rejected_before_store_io(
    limit, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents, _, _, _, _, gc = await setup_gc()
    listing = AsyncMock()
    monkeypatch.setattr(documents, "list_cleanup_candidates", listing)
    with pytest.raises(ValueError):
        await gc.sweep(limit=limit)
    listing.assert_not_awaited()


async def test_candidate_listing_failure_is_not_reported_as_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, _, _, _, _, gc = await setup_gc()
    monkeypatch.setattr(
        documents,
        "list_cleanup_candidates",
        AsyncMock(side_effect=ConnectionError("listing failed")),
    )
    with pytest.raises(ConnectionError, match="listing failed"):
        await gc.sweep()


async def test_non_superseded_result_is_rejected_without_abandoning_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, _, old, _, outcome, gc = await setup_gc()
    abandon = AsyncMock(wraps=documents.abandon_revision)
    monkeypatch.setattr(documents, "abandon_revision", abandon)
    with pytest.raises(ValueError):
        await gc.collect_superseded(replace(outcome, superseded=False))
    abandon.assert_not_awaited()
    assert (await documents.reserve_revision(old)).status is RevisionStatus.READY


async def test_failed_enrollment_before_commit_requires_retrying_original_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, old, _, outcome, gc = await setup_gc()
    with monkeypatch.context() as patch:
        patch.setattr(
            documents,
            "abandon_revision",
            AsyncMock(side_effect=ConnectionError("enrollment unavailable")),
        )
        with pytest.raises(ConnectionError, match="enrollment unavailable"):
            await gc.collect_superseded(outcome)
    assert (await documents.reserve_revision(old)).status is RevisionStatus.READY
    assert not (await gc.sweep()).completed
    await gc.collect_superseded(outcome)
    await assert_survivors(documents, vectors)


async def test_revision_cannot_reactivate_between_enrollment_and_vector_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, vectors, old, _, outcome, gc = await setup_gc()
    delete = vectors.delete
    reached = False

    async def try_reactivation(kb_id, *, filter):
        nonlocal reached
        reached = True
        with pytest.raises(RevisionNotReady):
            await documents.activate_revision(old.key)
        return await delete(kb_id, filter=filter)

    monkeypatch.setattr(vectors, "delete", try_reactivation)
    await gc.collect_superseded(outcome)
    assert reached
    await assert_survivors(documents, vectors)
