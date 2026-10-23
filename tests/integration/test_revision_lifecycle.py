from __future__ import annotations

from uuid import uuid4

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job

from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.task_store.postgres import PostgresTaskStore
from comet_rag.infrastructure.persistence.vector_store.milvus import AsyncMilvusStore
from comet_rag.infrastructure.task_execution.arq import ArqExecutor
from comet_rag.ports import NormalizedDocument, VectorRecord
from comet_rag.ports.document_store import RevisionKey, RevisionStatus
from comet_rag.services.indexing import IndexPlan, IndexPlanner, RevisionStager
from comet_rag.services.retrieval import (
    RetrievalService,
    ReturnLevel,
    SearchMode,
    SearchQuery,
)
from comet_rag.services.revision_activation import RevisionActivator
from comet_rag.services.revision_context import RevisionTaskStages, get_revision_key
from comet_rag.tasks import (
    Done,
    RetriableError,
    StagePipeline,
    TaskContext,
    TaskStatus,
)
from tests.contracts.knowledge_base import make_kb
from tests.contracts.support import wait_for_terminal, wait_until
from tests.integration.arq_support import _make_worker, _running
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration


class _Embedding(AsyncBaseEmbeddingModel):
    calls = 0

    def _vector(self, data, **kwargs) -> list[float]:
        return [1.0, 0.0]

    async def _embed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        return self._vector(data)

    async def close(self) -> None:
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
        await PostgresKnowledgeBaseRepository(database).create(
            make_kb(old.key.kb_id, embedding_dim=2)
        )
        await writer.ensure_collection(old.key.kb_id, dim=2)
        await writer.upsert(
            old.key.kb_id,
            [
                VectorRecord(
                    "legacy", "量子纠缠旧平坦记录", [1.0, 0.0], {"source_id": "source"}
                )
            ],
        )
        await stager.stage(old, embedding_dim=2)
        before = await service.search(
            SearchQuery(kb_id=old.key.kb_id, query="量子纠缠")
        )
        assert [hit.id for hit in before.chunks] == ["legacy"]
        await documents.activate_revision(old.key)
        await _assert_visible(service, old)

        upsert = writer.upsert

        async def lose_response(kb_id, records):
            await upsert(kb_id, records)
            raise ConnectionError("response lost after vector commit")

        monkeypatch.setattr(writer, "upsert", lose_response)
        with pytest.raises(ConnectionError, match="response lost"):
            await stager.stage(new, embedding_dim=2)
        await _assert_visible(service, old)
        monkeypatch.setattr(writer, "upsert", upsert)
        ready = await stager.stage(new, embedding_dim=2)
        assert ready.status is RevisionStatus.READY and ready.generation == 2
        await _assert_visible(service, old)
        await documents.activate_revision(new.key)
        await _assert_visible(service, new)
        assert (
            await reader.count(old.key.kb_id, filter={"document_revision": "old"}) == 4
        )
        # 再建独立连接仍看见新 pointer；这里没有 flush/sleep 等待可见性。
        assert await read_documents.get_active_revisions(old.key.kb_id, ["source"]) == {
            "source": "new"
        }
    finally:
        try:
            await writer.drop_collection(old.key.kb_id)
        finally:
            await reader.close()
            await writer.close()
            await read_database.close()
            await truncate_tables(database, "knowledge_bases")


@pytest.mark.parametrize(
    "publish", [False, True], ids=["ready", "activate-response-loss"]
)
async def test_revision_task_handoff_uses_only_persisted_context_across_workers(
    database: Database,
    postgres_dsn: str,
    redis_url: str,
    milvus_uri: str,
    milvus_database: str,
    monkeypatch: pytest.MonkeyPatch,
    publish: bool,
) -> None:
    await truncate_tables(database, "tasks", "knowledge_bases")
    unique = uuid4().hex[:12]
    kind = f"test-revision-preparation-{unique}"
    lanes = {"cpu": f"comet:test:{unique}:cpu", "io": f"comet:test:{unique}:io"}
    pool = await create_pool(RedisSettings.from_dsn(redis_url))
    cpu_db, io_db = Database(postgres_dsn), Database(postgres_dsn)
    producer_store = PostgresTaskStore(database)
    cpu_store, io_store = PostgresTaskStore(cpu_db), PostgresTaskStore(io_db)
    cpu = ArqExecutor(
        cpu_store,
        pool=pool,
        lanes=lanes,
        entry_lane="cpu",
        lane="cpu",
        retry_backoff=0.01,
    )
    io = ArqExecutor(
        io_store,
        pool=pool,
        lanes=lanes,
        entry_lane="cpu",
        lane="io",
        retry_backoff=0.01,
    )
    prefix = f"cthandoff_{unique}"
    cpu_vectors = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    io_vectors = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    cpu_model, io_model = _Embedding(), _Embedding()
    cpu_stages = RevisionTaskStages(
        RevisionStager(
            document_store=PostgresDocumentStore(cpu_db),
            vector_store=cpu_vectors,
            embedding_model=cpu_model,
            parent_batch_size=1,
        )
    )
    io_documents = PostgresDocumentStore(io_db)
    io_stages = RevisionTaskStages(
        RevisionStager(
            document_store=io_documents,
            vector_store=io_vectors,
            embedding_model=io_model,
            parent_batch_size=1,
            embed_window=1,
        ),
        activator=RevisionActivator(io_documents),
    )
    flow = StagePipeline()

    @flow.stage("chunking", lane="cpu")
    async def prepare(ctx: TaskContext) -> None:
        snapshot = await ctx.snapshot()
        if "revision_index" in snapshot.context:
            await cpu_stages.prepare(ctx)
            return
        key = await get_revision_key(ctx, kb_id="kb-revision-cycle", source_id="source")
        hierarchy = HierarchyBuilder(FixedSizeChunker(20), FixedSizeChunker(10)).split(
            NormalizedDocument(markdown=snapshot.context["text"])
        )
        plan = IndexPlanner().plan(
            hierarchy, key=key, source="fixture.md", file_type="md"
        )
        await cpu_stages.prepare(ctx, plan)

    @flow.stage("indexing", lane="io")
    async def stage(ctx: TaskContext) -> Done | None:
        try:
            ready = await io_stages.stage(ctx, embedding_dim=2)
        except ConnectionError as exc:
            raise RetriableError("模拟向量提交后响应丢失") from exc
        if publish:
            return None
        # 测试 kind 只承诺 prepare-to-ready，不代表生产 ingest 已完成激活。
        return Done(
            result={
                "revision_id": ready.key.revision_id,
                "status": ready.status,
                "generation": ready.generation,
            }
        )

    @flow.stage("activation", lane="io")
    async def activate(ctx: TaskContext) -> Done:
        try:
            outcome = await io_stages.activate(ctx)
        except ConnectionError as exc:
            raise RetriableError("模拟激活提交后响应丢失") from exc
        return Done(
            result={
                "revision_id": outcome.revision.key.revision_id,
                "status": outcome.revision.status,
                "generation": outcome.revision.generation,
                "already_active": outcome.already_active,
                "superseded": outcome.superseded,
            }
        )

    cpu.runners.register(kind)(flow)
    io.runners.register(kind)(flow)
    upsert = io_vectors.upsert
    failed = False

    async def lose_first_response(kb_id, records):
        nonlocal failed
        result = await upsert(kb_id, records)
        if not failed:
            failed = True
            raise ConnectionError("response lost after commit")
        return result

    monkeypatch.setattr(io_vectors, "upsert", lose_first_response)
    activate_revision = io_documents.activate_revision
    activation_failed = False

    async def lose_activation_response(key):
        nonlocal activation_failed
        result = await activate_revision(key)
        if not activation_failed:
            activation_failed = True
            raise ConnectionError("response lost after activation commit")
        return result

    if publish:
        monkeypatch.setattr(io_documents, "activate_revision", lose_activation_response)
    try:
        await PostgresKnowledgeBaseRepository(database).create(
            make_kb("kb-revision-cycle", embedding_dim=2)
        )
        task = await producer_store.create(
            kind, context={"text": "量子纠缠版本正文。" * 4}, max_attempts=3
        )
        async with _running(_make_worker(pool, lanes["cpu"], cpu, max_jobs=1)):
            await cpu.submit(task.task_id)

            async def handed_off() -> bool:
                snapshot = await producer_store.require(task.task_id)
                return (
                    snapshot.status is TaskStatus.PENDING
                    and snapshot.resume_stage == "indexing"
                    and await pool.zcard(lanes["io"]) == 1
                )

            await wait_until(handed_off, timeout=15, message="revision CPU 未完成移交")
        await cpu.shutdown(timeout=5)
        await cpu_db.close()
        await cpu_vectors.close()
        handed = await producer_store.require(task.task_id)
        assert handed.context["text"] is None and handed.attempts == 0
        revision = handed.context["revision_key"]["revision_id"]
        assert "parents" not in handed.context["revision_index"]
        assert cpu_model.calls == 0
        jobs = await pool.zrange(lanes["io"], 0, -1)
        info = await Job(jobs[0].decode(), redis=pool, _queue_name=lanes["io"]).info()
        assert info is not None and info.args == (task.task_id,) and info.kwargs == {}
        async with _running(_make_worker(pool, lanes["io"], io, max_jobs=1)):
            done = await wait_for_terminal(producer_store, task.task_id, timeout=20)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.attempts == (3 if publish else 2)
        assert done.result == {
            "revision_id": revision,
            "status": "active" if publish else "ready",
            "generation": 1,
            **({"already_active": True, "superseded": False} if publish else {}),
        }
        assert [s.stage for s in done.stage_history].count("chunking") == 1
        assert [s.stage for s in done.stage_history].count("indexing") == 2
        assert [s.stage for s in done.stage_history].count("activation") == (
            2 if publish else 0
        )
        assert (
            await io_vectors.count(
                "kb-revision-cycle",
                filter={"source_id": "source", "document_revision": revision},
            )
            == 4
        )
        assert io_model.calls == 5
        assert await PostgresDocumentStore(database).get_active_revisions(
            "kb-revision-cycle", ["source"]
        ) == {"source": revision if publish else None}
    finally:
        cpu.runners.unregister(kind)
        io.runners.unregister(kind)
        await cpu.shutdown(timeout=5)
        await io.shutdown(timeout=5)
        try:
            await io_vectors.drop_collection("kb-revision-cycle")
        finally:
            await cpu_vectors.close()
            await io_vectors.close()
            await cpu_db.close()
            await io_db.close()
            await pool.delete(*lanes.values())
            await pool.aclose()
            await truncate_tables(database, "tasks", "knowledge_bases")
