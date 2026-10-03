"""PostgresDocumentStore 运行与内存实现相同的契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.vector_store import InMemoryVectorStore
from comet_rag.ports import NormalizedDocument
from comet_rag.ports.document_store import DocumentStore, RevisionStatus, StaleRevision
from comet_rag.services.indexing import IndexPlan, IndexPlanner, RevisionStager
from comet_rag.services.retrieval import RetrievalService, ReturnLevel, SearchQuery
from tests.contracts.document_store import DocumentStoreContract, make_draft
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration


class TestPostgresDocumentStore(DocumentStoreContract):
    @pytest.fixture
    async def store(self, database: Database) -> AsyncIterator[DocumentStore]:
        await _require_migrated(database)
        await _truncate(database)
        repository = PostgresKnowledgeBaseRepository(database)
        await repository.acreate(make_kb(make_draft().key.kb_id))
        yield PostgresDocumentStore(database)
        await _truncate(database)

    async def test_reader_only_returns_active_revision_and_matching_parent(
        self,
        store: DocumentStore,
    ) -> None:
        vectors, model = InMemoryVectorStore(), _Embedding()
        writer = RevisionStager(
            document_store=store, vector_store=vectors, embedding_model=model
        )
        reader = RetrievalService(
            embedding_model=model, vector_store=vectors, document_store=store
        )
        old, new = _hierarchy_plan("old"), _hierarchy_plan("new")
        query = SearchQuery(
            kb_id=old.key.kb_id, query="q", return_level=ReturnLevel.PARENT
        )
        await writer.astage(old, embedding_dim=2)
        assert (await reader.search(query)).chunks == []
        await store.aactivate_revision(old.key)
        await writer.astage(new, embedding_dim=2)
        assert {
            item.metadata["document_revision"]
            for item in (await reader.search(query)).chunks
        } == {"old"}
        await store.aactivate_revision(new.key)
        result = await reader.search(query)
        assert {item.metadata["document_revision"] for item in result.chunks} == {"new"}
        assert {item.id for item in result.chunks} == {p.id for p in new.parents}
        assert {item.matched_child_id for item in result.chunks} <= {
            c.id for c in new.vector_children
        }
        assert await vectors.acount(old.key.kb_id) == 8

    async def test_planned_hierarchy_stages_and_replays(
        self, store: DocumentStore
    ) -> None:
        plan = _hierarchy_plan("staged")
        vectors = InMemoryVectorStore()
        writer = RevisionStager(
            document_store=store, vector_store=vectors, embedding_model=_Embedding()
        )
        ready = await writer.astage(plan, embedding_dim=2)
        replay = await writer.astage(plan, embedding_dim=2)
        assert ready.status is replay.status is RevisionStatus.READY
        assert ready.generation == replay.generation == 1
        parents = await store.aget_parents(plan.key.kb_id, [p.id for p in plan.parents])
        assert [parents[p.id].text for p in plan.parents] == ["abcdEF", "GHijkl"]
        assert await vectors.acount(plan.key.kb_id) == 4
        assert await store.aget_active_revisions(
            plan.key.kb_id, [plan.key.source_id]
        ) == {plan.key.source_id: None}

    async def test_staged_old_generation_cannot_replace_newer(
        self, store: DocumentStore
    ) -> None:
        old, new = _hierarchy_plan("old"), _hierarchy_plan("new")
        writer = RevisionStager(
            document_store=store,
            vector_store=InMemoryVectorStore(),
            embedding_model=_Embedding(),
        )
        await writer.astage(old, embedding_dim=2)
        await writer.astage(new, embedding_dim=2)
        await store.aactivate_revision(new.key)
        with pytest.raises(StaleRevision):
            await store.aactivate_revision(old.key)
        assert await store.aget_active_revisions(
            new.key.kb_id, [new.key.source_id]
        ) == {new.key.source_id: "new"}


def _hierarchy_plan(revision_id: str) -> IndexPlan:
    chunks = HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(3)).split(
        NormalizedDocument(markdown="abcdEFGHijkl")
    )
    return IndexPlanner().plan(
        chunks, key=make_draft(revision_id).key, source="fixture.md", file_type="md"
    )


class _Embedding(BaseEmbeddingModel):
    def _embed(self, data, **kwargs) -> list[float]:
        return [1.0, float(len(str(data)))]

    async def _aembed(self, data, **kwargs) -> list[float]:
        return self._embed(data)

    async def close_client(self) -> None:
        return None


async def _require_migrated(database: Database) -> None:
    async with database.session() as session:
        exists = (
            await session.execute(text("SELECT to_regclass('document_revisions')"))
        ).scalar_one()
    if exists is None:
        pytest.skip("document_revisions 表不存在，先跑 `uv run alembic upgrade head`")


async def _truncate(database: Database) -> None:
    await truncate_tables(database, "knowledge_bases")
