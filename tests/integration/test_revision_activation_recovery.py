from __future__ import annotations

from contextlib import asynccontextmanager
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
from comet_rag.ports.document_store import RevisionStatus
from comet_rag.services.indexing import IndexHandoff, RevisionStager
from comet_rag.services.retrieval import RetrievalService
from comet_rag.services.revision_activation import RevisionActivator
from tests.contracts.document_store import make_draft, make_ready
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables
from tests.integration.test_revision_lifecycle import _assert_visible, _Embedding, _plan

pytestmark = pytest.mark.integration


async def test_stale_ready_service_retry_keeps_newer_postgres_pointer(
    database: Database,
    postgres_dsn: str,
) -> None:
    await truncate_tables(database, "knowledge_bases")
    reader_db = Database(postgres_dsn)
    documents = PostgresDocumentStore(database)
    try:
        await PostgresKnowledgeBaseRepository(database).create(make_kb("kb-1"))
        old, _ = await make_ready(documents, make_draft("old"))
        new, _ = await make_ready(documents, make_draft("new"))
        await documents.activate_revision(new.key)
        for _ in range(2):
            outcome = await RevisionActivator(
                PostgresDocumentStore(reader_db)
            ).activate(old)
            assert outcome.superseded and not outcome.already_active
            assert outcome.revision.generation == 1
        assert await documents.get_active_revisions("kb-1", ["source-1"]) == {
            "source-1": "new"
        }
        assert (await documents.reserve_revision(old)).status is RevisionStatus.READY
    finally:
        await reader_db.close()
        await truncate_tables(database, "knowledge_bases")


@pytest.mark.parametrize("fault", ["rollback", "lost-response", "lost-response-newer"])
async def test_activation_recovery_with_independent_postgres_and_strong_milvus_readers(
    database: Database,
    postgres_dsn: str,
    milvus_uri: str,
    milvus_database: str,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    await truncate_tables(database, "knowledge_bases")
    prefix = f"ctactivate_{uuid4().hex[:12]}"
    writer = AsyncMilvusStore(
        endpoint=milvus_uri, database_name=milvus_database, prefix=prefix
    )
    reader = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    read_database = Database(postgres_dsn)
    documents = PostgresDocumentStore(database)
    read_documents = PostgresDocumentStore(read_database)
    embedding = _Embedding()
    old = _plan("old", "量子纠缠旧版本说明。" * 2)
    target = _plan("target", "量子纠缠待激活版本。" * 2)
    newer = _plan("newer", "量子纠缠更新版本。" * 2)
    service = RetrievalService(
        embedding_model=embedding,
        vector_store=reader,
        keyword_search=reader,
        document_store=read_documents,
    )
    stager = RevisionStager(
        document_store=documents, vector_store=writer, embedding_model=embedding
    )
    draft = IndexHandoff.from_plan(target).draft
    activate = documents.activate_revision
    transaction = database.transaction

    @asynccontextmanager
    async def rollback_before_commit():
        async with transaction() as session:
            yield session
            # Store 的两次 flush 均已执行；异常必须回滚状态和 active pointer。
            raise ConnectionError("activation transaction rolled back")

    async def fail_activation(key):
        if fault == "rollback":
            with monkeypatch.context() as patch:
                patch.setattr(database, "transaction", rollback_before_commit)
                return await activate(key)
        await activate(key)
        raise ConnectionError("activation committed but response lost")

    try:
        await PostgresKnowledgeBaseRepository(database).create(
            make_kb(old.key.kb_id, embedding_dim=2)
        )
        await stager.stage(old, embedding_dim=2)
        await activate(old.key)
        await stager.stage(target, embedding_dim=2)
        monkeypatch.setattr(documents, "activate_revision", fail_activation)
        with pytest.raises(ConnectionError, match="activation"):
            await RevisionActivator(documents).activate(draft)
        expected = old if fault == "rollback" else target
        await _assert_visible(service, expected)
        state = await documents.reserve_revision(draft)
        assert state.generation == 2
        assert state.status is (
            RevisionStatus.READY if fault == "rollback" else RevisionStatus.ACTIVE
        )
        if fault == "lost-response-newer":
            await stager.stage(newer, embedding_dim=2)
            await activate(newer.key)
        monkeypatch.setattr(documents, "activate_revision", activate)
        calls = embedding.calls
        # 新服务、独立连接，仅凭原 manifest 恢复，不重做向量写入。
        outcome = await RevisionActivator(read_documents).activate(draft)
        assert outcome.revision.generation == 2
        assert outcome.superseded is (fault == "lost-response-newer")
        assert outcome.already_active is (fault == "lost-response")
        if fault != "rollback":
            assert outcome.revision.activated_at == state.activated_at
        assert embedding.calls == calls
        await _assert_visible(service, newer if outcome.superseded else target)
        assert await reader.count(
            old.key.kb_id, filter={"document_revision": "old"}
        ) == len(old.vector_children)
        assert await reader.count(
            target.key.kb_id, filter={"document_revision": "target"}
        ) == len(target.vector_children)
    finally:
        try:
            await writer.drop_collection(old.key.kb_id)
        finally:
            await reader.close()
            await writer.close()
            await read_database.close()
            await truncate_tables(database, "knowledge_bases")
