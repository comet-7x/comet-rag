from __future__ import annotations

from uuid import uuid4

import pytest

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
from comet_rag.infrastructure.persistence.vector_store.milvus import MilvusStore
from comet_rag.ports import NormalizedDocument, VectorRecord
from comet_rag.ports.document_store import RevisionKey, RevisionStatus
from comet_rag.services.indexing import IndexPlan, IndexPlanner, RevisionStager
from comet_rag.services.retrieval import (
    RetrievalService,
    ReturnLevel,
    SearchMode,
    SearchQuery,
)
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration


class _Embedding(BaseEmbeddingModel):
    def _embed(self, data, **kwargs) -> list[float]:
        return [1.0, 0.0]

    async def _aembed(self, data, **kwargs) -> list[float]:
        return self._embed(data)

    async def close_client(self) -> None:
        return None


def _plan(revision: str, text: str) -> IndexPlan:
    hierarchy = HierarchyBuilder(FixedSizeChunker(20), FixedSizeChunker(10)).split(
        NormalizedDocument(markdown=text)
    )
    return IndexPlanner().plan(
        hierarchy,
        key=RevisionKey("kb-revision-cycle", "source", revision),
        source="fixture.md",
        file_type="md",
    )


async def _assert_visible(service: RetrievalService, plan: IndexPlan) -> None:
    for mode in SearchMode:
        for level in ReturnLevel:
            result = await service.search(
                SearchQuery(
                    kb_id=plan.key.kb_id,
                    query="量子纠缠",
                    mode=mode,
                    return_level=level,
                    top_k=10,
                    rerank=False,
                )
            )
            expected = (
                plan.parents if level is ReturnLevel.PARENT else plan.vector_children
            )
            assert {hit.id for hit in result.chunks} == {chunk.id for chunk in expected}
            assert {hit.text for hit in result.chunks} == {
                chunk.text for chunk in expected
            }
            assert all(
                hit.metadata["document_revision"] == plan.key.revision_id
                for hit in result.chunks
            )
            assert not result.degradations
            if level is ReturnLevel.PARENT:
                assert all(
                    hit.matched_child_id in {c.id for c in plan.vector_children}
                    for hit in result.chunks
                )


async def test_pg_pointer_and_independent_strong_milvus_survive_partial_write(
    database: Database,
    postgres_dsn: str,
    milvus_uri: str,
    milvus_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await truncate_tables(database, "knowledge_bases")
    prefix = f"ctcycle_{uuid4().hex[:12]}"
    writer = MilvusStore(
        endpoint=milvus_uri, database_name=milvus_database, prefix=prefix
    )
    reader = MilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    read_database = Database(postgres_dsn)
    documents = PostgresDocumentStore(database)
    read_documents = PostgresDocumentStore(read_database)
    embedding = _Embedding()
    old = _plan("old", "量子纠缠旧版本说明。" * 4)
    new = _plan("new", "量子纠缠新版本说明。" * 2)
    service = RetrievalService(
        embedding_model=embedding,
        vector_store=reader,
        keyword_search=reader,
        document_store=read_documents,
    )
    stager = RevisionStager(
        document_store=documents,
        vector_store=writer,
        embedding_model=embedding,
        parent_batch_size=1,
        embed_window=1,
    )
    try:
        await PostgresKnowledgeBaseRepository(database).acreate(
            make_kb(old.key.kb_id, embedding_dim=2)
        )
        await writer.aensure_collection(old.key.kb_id, dim=2)
        await writer.aupsert(
            old.key.kb_id,
            [
                VectorRecord(
                    "legacy", "量子纠缠旧平坦记录", [1.0, 0.0], {"source_id": "source"}
                )
            ],
        )
        await stager.astage(old, embedding_dim=2)
        before = await service.search(
            SearchQuery(kb_id=old.key.kb_id, query="量子纠缠")
        )
        assert [hit.id for hit in before.chunks] == ["legacy"]
        await documents.aactivate_revision(old.key)
        await _assert_visible(service, old)

        upsert = writer.aupsert

        async def lose_response(kb_id, records):
            await upsert(kb_id, records)
            raise ConnectionError("response lost after vector commit")

        monkeypatch.setattr(writer, "aupsert", lose_response)
        with pytest.raises(ConnectionError, match="response lost"):
            await stager.astage(new, embedding_dim=2)
        await _assert_visible(service, old)
        monkeypatch.setattr(writer, "aupsert", upsert)
        ready = await stager.astage(new, embedding_dim=2)
        assert ready.status is RevisionStatus.READY and ready.generation == 2
        await _assert_visible(service, old)
        await documents.aactivate_revision(new.key)
        await _assert_visible(service, new)
        assert (
            await reader.acount(old.key.kb_id, filter={"document_revision": "old"}) == 4
        )
        # 再建独立连接仍看见新 pointer；这里没有 flush/sleep 等待可见性。
        assert await read_documents.aget_active_revisions(
            old.key.kb_id, ["source"]
        ) == {"source": "new"}
    finally:
        try:
            await writer.adrop_collection(old.key.kb_id)
        finally:
            await reader.aclose()
            await writer.aclose()
            await read_database.aclose()
            await truncate_tables(database, "knowledge_bases")
