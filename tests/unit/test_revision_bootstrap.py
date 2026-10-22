from __future__ import annotations

from unittest.mock import Mock

import pytest

from comet_rag.composition import bootstrap
from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    RetrievalConfig,
    ServerConfig,
    SqlDatabaseConfig,
    VectorDatabaseConfig,
)
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.infrastructure.persistence.vector_store.milvus import AsyncMilvusStore
from comet_rag.ports import VectorRecord
from comet_rag.ports.document_store import RevisionDraft, RevisionKey
from comet_rag.services.retrieval import SearchQuery
from comet_rag.tasks import InMemoryTaskStore


class Embedding(AsyncBaseEmbeddingModel):
    def _vector(self, data, **kwargs) -> list[float]:
        return [1.0, 0.0, 0.0]

    async def _embed(self, data, **kwargs) -> list[float]:
        return self._vector(data)

    async def close(self) -> None:
        return None


def config(
    *, enabled: bool = True, milvus: bool = False, budget: int = 2048
) -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=8000),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://fake/v1", model_name="fake", dim=3
            ),
            database=SqlDatabaseConfig(
                host="fake",
                port=5432,
                username="comet",
                password="test",  # noqa: S106 — 测试占位口令
                database="test",
            )
            if milvus
            else None,
            vector_database=VectorDatabaseConfig(
                endpoint="http://fake:19530", database_name="test"
            )
            if milvus
            else None,
        ),
        backends=BackendsConfig(
            vector_store=Backend.MILVUS if milvus else Backend.MEMORY,
            task_store=Backend.POSTGRES if milvus else Backend.MEMORY,
        ),
        retrieval=RetrievalConfig(
            revision_aware=enabled, max_revision_candidates=budget
        ),
    )


async def test_disabled_read_switch_never_builds_document_store(monkeypatch) -> None:
    builder = Mock(
        side_effect=AssertionError("disabled read must not create DocumentStore")
    )
    monkeypatch.setattr(bootstrap, "build_document_store", builder)
    context = await bootstrap.build_context(
        config(enabled=False), embedding_model=Embedding()
    )
    assert context.document_store is None
    builder.assert_not_called()
    await context.close()


async def test_enabled_memory_read_is_shared_and_preserves_legacy_visibility() -> None:
    context = await bootstrap.build_context(
        config(budget=64), embedding_model=Embedding()
    )
    try:
        documents = context.document_store
        assert isinstance(documents, InMemoryDocumentStore)
        assert context.database is None
        assert context.retrieval._document_store is documents  # noqa: SLF001
        await context.vector_store.ensure_collection("kb", dim=3)
        from comet_rag.services.knowledge_base import KnowledgeBaseSpec

        await context.knowledge_base.create(KnowledgeBaseSpec(kb_id="kb"))
        await context.vector_store.upsert(
            "kb",
            [
                VectorRecord(
                    "legacy",
                    "legacy body",
                    [1.0, 0.0, 0.0],
                    {"source_id": "source"},
                )
            ],
        )
        query = SearchQuery(kb_id="kb", query="q")
        assert [hit.id for hit in (await context.retrieval.search(query)).chunks] == [
            "legacy"
        ]
        key = RevisionKey("kb", "source", "new")
        await documents.reserve_revision(
            RevisionDraft(key, "a" * 64, 0, 1, "fixture.md", "md")
        )
        await documents.mark_ready(key)
        await context.vector_store.upsert(
            "kb",
            [
                VectorRecord(
                    "active",
                    "active body",
                    [1.0, 0.0, 0.0],
                    {"source_id": "source", "document_revision": "new"},
                )
            ],
        )
        await documents.activate_revision(key)
        assert [hit.id for hit in (await context.retrieval.search(query)).chunks] == [
            "active"
        ]
        recall = context.retrieval._revision_recall  # noqa: SLF001 — 配置预算必须落到调用边界
        assert recall is not None and recall._cap == 64  # noqa: SLF001
    finally:
        await context.close()


@pytest.mark.parametrize("milvus", [False, True])
async def test_injected_memory_vectors_pair_with_memory_documents_without_extra_database(
    monkeypatch,
    milvus: bool,
) -> None:
    builder = Mock(
        side_effect=AssertionError("all injected stores need no new SQL pool")
    )
    monkeypatch.setattr(bootstrap, "build_database", builder)
    context = await bootstrap.build_context(
        config(milvus=milvus),
        embedding_model=Embedding(),
        vector_store=AsyncInMemoryVectorStore(),
        task_store=InMemoryTaskStore(),
        kb_repository=InMemoryKnowledgeBaseRepository(),
    )
    try:
        assert isinstance(context.document_store, InMemoryDocumentStore)
        assert context.database is None
        builder.assert_not_called()
    finally:
        await context.close()


class FakeMilvus(AsyncMilvusStore):
    def __init__(self, consistency: str = "Strong") -> None:
        self._consistency = consistency

    async def close(self) -> None:
        return None


async def test_session_injected_without_documents_is_rejected_before_sql_creation(
    monkeypatch,
) -> None:
    builder = Mock(
        side_effect=AssertionError("preflight must precede SQL pool creation")
    )
    monkeypatch.setattr(bootstrap, "build_database", builder)
    with pytest.raises(ValueError, match="Strong"):
        await bootstrap.build_context(
            config(milvus=True), vector_store=FakeMilvus("Session")
        )
    builder.assert_not_called()


@pytest.mark.parametrize("fault", ["memory-docs", "session", "postgres-with-memory"])
async def test_bad_injected_revision_pair_is_rejected_before_model_or_database_creation(
    monkeypatch, fault: str
) -> None:
    builder = Mock(side_effect=AssertionError("resource preflight must run first"))
    monkeypatch.setattr(bootstrap, "build_embedding_model", builder)
    monkeypatch.setattr(bootstrap, "build_database", builder)
    vectors = (
        AsyncInMemoryVectorStore()
        if fault == "postgres-with-memory"
        else FakeMilvus("Session" if fault == "session" else "Strong")
    )
    documents = (
        InMemoryDocumentStore()
        if fault == "memory-docs"
        else PostgresDocumentStore(Mock(spec=Database))
    )
    with pytest.raises(
        ValueError, match="(InMemoryDocumentStore|PostgresDocumentStore|Strong)"
    ):
        await bootstrap.build_context(
            config(), vector_store=vectors, document_store=documents
        )
    builder.assert_not_called()


async def test_document_only_postgres_requirement_is_not_lost_when_other_stores_are_injected(
    monkeypatch,
) -> None:
    database = Mock(spec=Database)
    builder = Mock(return_value=database)
    monkeypatch.setattr(bootstrap, "build_database", builder)
    context = await bootstrap.build_context(
        config(milvus=True),
        embedding_model=Embedding(),
        vector_store=FakeMilvus(),
        task_store=InMemoryTaskStore(),
        kb_repository=InMemoryKnowledgeBaseRepository(),
    )
    assert isinstance(context.document_store, PostgresDocumentStore)
    assert context.document_store._db is database  # noqa: SLF001
    assert context.database is database
    builder.assert_called_once()
    await context.close()
    database.close.assert_awaited_once()
