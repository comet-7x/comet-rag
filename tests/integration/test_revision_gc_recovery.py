from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.vector_store.milvus import AsyncMilvusStore
from comet_rag.ports import VectorRecord
from comet_rag.ports.document_store import RevisionStatus
from comet_rag.services.indexing import IndexHandoff, IndexPlan, RevisionStager
from comet_rag.services.retrieval import RetrievalService
from comet_rag.services.revision_activation import ActivationOutcome, RevisionActivator
from comet_rag.services.revision_gc import RevisionGarbageCollector
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables
from tests.integration.test_revision_lifecycle import _assert_visible, _Embedding, _plan

pytestmark = pytest.mark.integration


@dataclass(slots=True)
class Environment:
    documents: PostgresDocumentStore
    read_documents: PostgresDocumentStore
    writer: AsyncMilvusStore
    reader: AsyncMilvusStore
    service: RetrievalService
    stale: IndexPlan
    old: IndexPlan
    active: IndexPlan
    outcome: ActivationOutcome

    def collector(self) -> RevisionGarbageCollector:
        return RevisionGarbageCollector(
            document_store=self.read_documents, vector_store=self.reader
        )


@pytest.fixture
async def gc_env(
    database: Database, postgres_dsn: str, milvus_uri: str, milvus_database: str
):
    await truncate_tables(database, "knowledge_bases")
    prefix = f"ctgc_{uuid4().hex[:12]}"
    writer = AsyncMilvusStore(
        endpoint=milvus_uri, database_name=milvus_database, prefix=prefix
    )
    reader = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    reader_db = Database(postgres_dsn)
    documents, read_documents = (
        PostgresDocumentStore(database),
        PostgresDocumentStore(reader_db),
    )
    embedding = _Embedding()
    stager = RevisionStager(
        document_store=documents, vector_store=writer, embedding_model=embedding
    )
    # 每个十字符 child 都含检索词，不能要求 BM25 召回不含词的尾块。
    stale, old, active = [
        _plan(name, text * 2)
        for name, text in (
            ("stale", "量子纠缠待版本说明。"),
            ("old", "量子纠缠旧版本说明。"),
            ("active", "量子纠缠新版本说明。"),
        )
    ]
    try:
        await PostgresKnowledgeBaseRepository(database).create(
            make_kb(stale.key.kb_id, embedding_dim=2)
        )
        await stager.stage(stale, embedding_dim=2)
        for plan in (old, active):
            await stager.stage(plan, embedding_dim=2)
            await documents.activate_revision(plan.key)
        outcome = await RevisionActivator(documents).activate(
            IndexHandoff.from_plan(stale).draft
        )
        await writer.upsert(
            stale.key.kb_id,
            [
                VectorRecord(
                    "legacy", "量子纠缠legacy", [1.0, 0.0], {"source_id": "source"}
                ),
                VectorRecord(
                    "other-source",
                    "量子纠缠other",
                    [1.0, 0.0],
                    {"source_id": "other", "document_revision": "stale"},
                ),
            ],
        )
        service = RetrievalService(
            embedding_model=embedding,
            vector_store=reader,
            keyword_search=reader,
            document_store=read_documents,
        )
        await _assert_visible(service, active)
        yield Environment(
            documents,
            read_documents,
            writer,
            reader,
            service,
            stale,
            old,
            active,
            outcome,
        )
    finally:
        try:
            await writer.drop_collection(stale.key.kb_id)
        finally:
            await reader.close()
            await writer.close()
            await reader_db.close()
            await truncate_tables(database, "knowledge_bases")


async def assert_clean(env: Environment) -> None:
    await _assert_visible(env.service, env.active)
    assert (
        await env.reader.count(env.active.key.kb_id)
        == len(env.active.vector_children) + 2
    )
    assert (
        await env.reader.count(
            env.active.key.kb_id,
            filter={"source_id": "other", "document_revision": "stale"},
        )
        == 1
    )
    assert (
        await env.read_documents.get_parents(
            env.stale.key.kb_id, [p.id for p in (*env.stale.parents, *env.old.parents)]
        )
        == {}
    )
    assert await env.documents.list_cleanup_candidates(limit=32) == []
    for plan in (env.stale, env.old):
        row = await env.documents.reserve_revision(IndexHandoff.from_plan(plan).draft)
        assert row.status is RevisionStatus.PURGED


@pytest.mark.parametrize(
    "fault",
    [
        "partial-delete",
        "silent-partial",
        "delete-response",
        "purge-rollback",
        "purge-response",
        "abandon-response",
    ],
)
async def test_partial_gc_recovers_from_durable_candidates_without_touching_active(
    gc_env: Environment,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    env = gc_env
    collector = RevisionGarbageCollector(
        document_store=env.documents, vector_store=env.writer
    )
    delete = env.writer.delete
    purge, abandon = (
        env.documents.purge_inactive_revision,
        env.documents.abandon_revision,
    )
    transaction = database.transaction

    @asynccontextmanager
    async def rollback():
        async with transaction() as session:
            yield session
            raise ConnectionError("purge transaction rollback")

    async def bad_delete(kb_id, *, filter):
        if fault in ("partial-delete", "silent-partial"):
            result = await delete(
                kb_id, ids=[env.stale.vector_children[0].id], filter=filter
            )
            if fault == "silent-partial":
                return result
        else:
            await delete(kb_id, filter=filter)
        raise ConnectionError("delete response lost")

    async def bad_purge(key):
        if fault == "purge-rollback":
            with monkeypatch.context() as patch:
                patch.setattr(database, "transaction", rollback)
                return await purge(key)
        await purge(key)
        raise ConnectionError("purge response lost")

    async def bad_abandon(key):
        await abandon(key)
        raise ConnectionError("abandon response lost")

    with monkeypatch.context() as patch:
        if "delete" in fault or fault == "silent-partial":
            patch.setattr(env.writer, "delete", bad_delete)
        elif fault.startswith("purge"):
            patch.setattr(env.documents, "purge_inactive_revision", bad_purge)
        else:
            patch.setattr(env.documents, "abandon_revision", bad_abandon)
        with pytest.raises((ConnectionError, RuntimeError)):
            await collector.collect_superseded(env.outcome)
    await _assert_visible(env.service, env.active)
    stale = await env.documents.reserve_revision(
        IndexHandoff.from_plan(env.stale).draft
    )
    assert stale.status is (
        RevisionStatus.PURGED if fault == "purge-response" else RevisionStatus.ABANDONED
    )
    if fault != "purge-response":
        assert len(
            await env.read_documents.get_parents(
                env.stale.key.kb_id, [p.id for p in env.stale.parents]
            )
        ) == len(env.stale.parents)
    # 新连接实例不依赖进程内失败状态；未被标记 purged 的候选都能恢复。
    report = await env.collector().sweep()
    assert not report.failed
    assert set(report.completed) == (
        {env.old.key} if fault == "purge-response" else {env.stale.key, env.old.key}
    )
    await env.collector().collect_superseded(env.outcome)
    await assert_clean(env)


async def test_concurrent_collectors_and_repeated_cleanup_preserve_active(
    gc_env: Environment,
) -> None:
    env = gc_env
    await env.documents.abandon_revision(env.stale.key)
    reports = await asyncio.gather(env.collector().sweep(), env.collector().sweep())
    assert all(not report.failed for report in reports)
    assert {key for report in reports for key in report.completed} == {
        env.stale.key,
        env.old.key,
    }
    assert not (await env.collector().sweep()).completed
    await assert_clean(env)


async def test_real_active_guard_precedes_milvus_io_even_for_incorrect_candidate(
    gc_env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = gc_env
    monkeypatch.setattr(
        env.read_documents,
        "list_cleanup_candidates",
        AsyncMock(return_value=[env.active.key]),
    )
    delete = AsyncMock(wraps=env.reader.delete)
    monkeypatch.setattr(env.reader, "delete", delete)
    report = await env.collector().sweep()
    assert report.failed == (env.active.key,) and not report.completed
    delete.assert_not_awaited()
    await _assert_visible(env.service, env.active)
