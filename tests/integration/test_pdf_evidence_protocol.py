from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from comet_rag.infrastructure.evidence.bundle import (
    EvidenceReference,
    build_bundle,
    load_bundle,
    publish_bundle,
)
from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.task_store.postgres import PostgresTaskStore
from comet_rag.ports.document_store import RevisionDraft, RevisionKey, StoredParent
from comet_rag.tasks import TaskBusy, TaskContext, TaskStatus
from comet_rag.tasks.store import TaskLeaseLost
from evals.pdf.capture_journal import (
    BUNDLE_COMMITTED,
    INTENT,
    OBSERVED,
    AsyncCaptureJournal,
    guard_capture_task_deletion,
)
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration
KEY = RevisionKey("isolated-evidence", "source", "revision")
SOURCE = "a" * 64
ENDPOINT = "c" * 64


def bundle():
    raw = json.dumps(
        {
            "metadata": {
                "file_suffix": "pdf",
                "producer": {"name": "mineru", "version": "4.0.12"},
                "document": {"page_count": 1, "page_count_kind": "physical"},
            },
            "is_full_document": True,
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "text",
                            "bbox": [0.1, 0.1, 0.9, 0.9],
                            "content": "A source fact",
                        }
                    ],
                }
            ],
        }
    ).encode()
    return build_bundle(raw, key=KEY, source_sha256=SOURCE, pages=1)


async def context(store: PostgresTaskStore) -> TaskContext:
    task = await store.create("isolated-evidence-protocol")
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="worker", attempts=1
    )
    return TaskContext(store, task.task_id, worker_id="worker", attempts=1)


async def test_postgres_lost_confirmation_reopens_bundle_and_parent_reference(
    database: Database,
    postgres_dsn: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    await truncate_tables(database, "tasks", "knowledge_bases")
    other = Database(postgres_dsn)
    try:
        store = PostgresTaskStore(database)
        ctx = await context(store)
        journal = AsyncCaptureJournal(ctx)
        assert await journal.begin(
            key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT
        )
        await journal.observe(
            task_id="job_one",
            source_file_id="file-one",
            intent=(await ctx.snapshot()).context[INTENT],
        )
        await journal.verify_source(observed=(await ctx.snapshot()).context[OBSERVED])
        packet = bundle()
        await journal.plan_bundle(packet.reference)
        publish_bundle(packet, tmp_path / "bundle")
        original = store._save
        lost = False

        async def lose_confirmation(task, expected_version, **kwargs):
            nonlocal lost
            saved = await original(task, expected_version, **kwargs)
            if BUNDLE_COMMITTED in task.context and not lost:
                lost = True
                raise OSError("committed then reply lost")
            return saved

        monkeypatch.setattr(store, "_save", lose_confirmation)
        with pytest.raises(OSError, match="reply lost"):
            await journal.commit_local_bundle(packet.reference, tmp_path / "bundle")
        fresh_store = PostgresTaskStore(other)
        fresh = TaskContext(fresh_store, ctx.task_id, worker_id="worker", attempts=1)
        restored = AsyncCaptureJournal(fresh)
        assert not await restored.begin(
            key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT
        )
        await restored.commit_local_bundle(packet.reference, tmp_path / "bundle")
        assert (await fresh.snapshot()).context[
            BUNDLE_COMMITTED
        ] == packet.reference.payload()

        await PostgresKnowledgeBaseRepository(database).create(make_kb(KEY.kb_id))
        documents = PostgresDocumentStore(database)
        await documents.reserve_revision(
            RevisionDraft(KEY, "b" * 64, 1, 0, "test.pdf", "pdf")
        )
        await documents.put_parents(
            KEY,
            [
                StoredParent(
                    KEY,
                    "parent",
                    0,
                    "A source fact",
                    0,
                    13,
                    metadata={"evidence_reference": packet.reference.payload()},
                )
            ],
        )
        await documents.mark_ready(KEY)
        await documents.activate_revision(KEY)
        await database.close()
        parent = (
            await PostgresDocumentStore(other).get_parents(KEY.kb_id, ["parent"])
        )["parent"]
        ref = EvidenceReference.from_payload(parent.metadata["evidence_reference"])
        assert load_bundle(tmp_path / "bundle", ref).document.markdown == parent.text
        await fresh_store.transition(ctx.task_id, TaskStatus.FAILED)
        with pytest.raises(TaskBusy):
            await fresh_store.delete(
                ctx.task_id, force=True, guard=guard_capture_task_deletion
            )
    finally:
        await truncate_tables(other, "tasks", "knowledge_bases")
        await other.close()


async def test_postgres_cas_keeps_one_intent_and_fences_old_attempt(
    database: Database,
    postgres_dsn: str,
) -> None:
    await truncate_tables(database, "tasks")
    other = Database(postgres_dsn)
    try:
        store = PostgresTaskStore(database)
        ctx = await context(store)
        concurrent = TaskContext(
            PostgresTaskStore(other), ctx.task_id, worker_id="worker", attempts=1
        )
        journals = [AsyncCaptureJournal(ctx), AsyncCaptureJournal(concurrent)]
        results = await asyncio.gather(
            *(
                j.begin(key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT)
                for j in journals
            )
        )
        assert sorted(results) == [False, True]
        intent = (await ctx.snapshot()).context[INTENT]
        await store.update(ctx.task_id, attempts=2)
        with pytest.raises(TaskLeaseLost):
            await journals[1].observe(
                task_id="job_late", source_file_id="file-late", intent=intent
            )
        assert OBSERVED not in (await ctx.snapshot()).context
    finally:
        await truncate_tables(database, "tasks")
        await other.close()
