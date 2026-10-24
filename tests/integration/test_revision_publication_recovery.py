from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest

from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.task_store.postgres import PostgresTaskStore
from comet_rag.infrastructure.persistence.vector_store.milvus import AsyncMilvusStore
from comet_rag.ports import VectorRecord
from comet_rag.ports.document_store import RevisionStatus
from comet_rag.services.indexing import IndexHandoff, IndexPlan, RevisionStager
from comet_rag.services.retrieval import RetrievalService
from comet_rag.services.revision_context import RevisionTaskStages, get_revision_key
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.tasks import TaskContext, TaskStatus
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables
from tests.integration.test_revision_lifecycle import _assert_visible, _Embedding, _plan

pytestmark = pytest.mark.integration


@dataclass(slots=True)
class Environment:
    documents: PostgresDocumentStore
    read_documents: PostgresDocumentStore
    tasks: PostgresTaskStore
    read_tasks: PostgresTaskStore
    writer: AsyncMilvusStore
    reader: AsyncMilvusStore
    prefix: str
    model: _Embedding
    ctx: TaskContext
    plan: IndexPlan
    service: RetrievalService

    def stages(self, *, independent: bool = False) -> RevisionTaskStages:
        documents = self.read_documents if independent else self.documents
        vectors = self.reader if independent else self.writer
        return RevisionTaskStages(
            RevisionStager(
                document_store=documents,
                vector_store=vectors,
                embedding_model=self.model,
            ),
            document_store=documents,
            garbage_collector=RevisionGarbageCollector(
                document_store=documents, vector_store=vectors
            ),
        )

    def fresh_context(self) -> TaskContext:
        return TaskContext(
            self.read_tasks, self.ctx.task_id, worker_id="test", attempts=1
        )

    async def replace(self) -> IndexPlan:
        plan = _plan("winner", "量子纠缠新版本说明。" * 2)
        await RevisionStager(
            document_store=self.documents,
            vector_store=self.writer,
            embedding_model=self.model,
        ).stage(plan, embedding_dim=2)
        await self.documents.activate_revision(plan.key)
        await _assert_visible(self.service, plan)
        return plan


@pytest.fixture
async def publication_env(
    database: Database, postgres_dsn: str, milvus_uri: str, milvus_database: str
):
    await truncate_tables(database, "tasks", "knowledge_bases")
    prefix = "ctpublication_" + uuid4().hex[:12]
    writer = AsyncMilvusStore(
        endpoint=milvus_uri, database_name=milvus_database, prefix=prefix
    )
    reader = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    read_db = Database(postgres_dsn)
    documents, read_documents = (
        PostgresDocumentStore(database),
        PostgresDocumentStore(read_db),
    )
    tasks, read_tasks = PostgresTaskStore(database), PostgresTaskStore(read_db)
    model = _Embedding()
    plan = None
    try:
        await PostgresKnowledgeBaseRepository(database).create(
            make_kb("kb-revision-cycle", embedding_dim=2)
        )
        task = await tasks.create("revision-publication-test")
        await tasks.transition(
            task.task_id, TaskStatus.RUNNING, worker_id="test", attempts=1
        )
        ctx = TaskContext(tasks, task.task_id, worker_id="test", attempts=1)
        key = await get_revision_key(ctx, kb_id="kb-revision-cycle", source_id="source")
        plan = _plan(key.revision_id, "量子纠缠待版本说明。" * 2)
        env = Environment(
            documents,
            read_documents,
            tasks,
            read_tasks,
            writer,
            reader,
            prefix,
            model,
            ctx,
            plan,
            RetrievalService(
                embedding_model=model,
                vector_store=reader,
                keyword_search=reader,
                document_store=read_documents,
            ),
        )
        stages = env.stages()
        await stages.prepare(ctx, plan)
        await stages.stage(ctx, embedding_dim=2)
        yield env
    finally:
        try:
            if plan is not None:
                await writer.drop_collection(plan.key.kb_id)
        finally:
            await reader.close()
            await writer.close()
            await read_db.close()
            await truncate_tables(database, "tasks", "knowledge_bases")


@pytest.mark.parametrize("superseded", [False, True])
async def test_activation_result_save_failure_recovers_using_independent_stores(
    publication_env: Environment, superseded: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = publication_env
    winner = await env.replace() if superseded else env.plan
    put = env.ctx.put_bounded

    async def fail_result(limit, **values):
        if values.get("revision_publication", {}).get("phase") != "activating":
            raise ConnectionError("publication result write failed")
        await put(limit, **values)

    with monkeypatch.context() as patch:
        patch.setattr(env.ctx, "put_bounded", fail_result)
        with pytest.raises(ConnectionError):
            await env.stages().activate(env.ctx)
    persisted = await env.read_tasks.require(env.ctx.task_id)
    assert persisted.context["revision_publication"]["phase"] == "activating"
    await _assert_visible(env.service, winner)
    before = await env.read_documents.reserve_revision(
        IndexHandoff.from_plan(env.plan, task_id=env.ctx.task_id).draft
    )
    calls = env.model.calls
    outcome = await env.stages(independent=True).activate(env.fresh_context())
    assert outcome.superseded is superseded
    assert outcome.revision.generation == 1
    assert outcome.revision.activated_at == before.activated_at
    assert env.model.calls == calls
    assert (await env.read_tasks.require(env.ctx.task_id)).context[
        "revision_publication"
    ]["phase"] == ("cleanup_pending" if superseded else "active")
    await _assert_visible(env.service, winner)


@pytest.mark.parametrize(
    "fault",
    ["abandon-before", "abandon-after", "purge-after", "ack-before", "ack-after"],
)
async def test_cleanup_intent_survives_gc_and_task_ack_failures(
    publication_env: Environment, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = publication_env
    winner = await env.replace()
    await env.stages().activate(env.ctx)
    abandon, purge, put = (
        env.documents.abandon_revision,
        env.documents.purge_inactive_revision,
        env.ctx.put_bounded,
    )

    async def bad_abandon(key):
        if fault == "abandon-after":
            await abandon(key)
        raise ConnectionError("abandon failed")

    async def bad_purge(key):
        await purge(key)
        raise ConnectionError("purge response lost")

    async def bad_ack(limit, **values):
        if fault == "ack-after":
            await put(limit, **values)
        raise ConnectionError("cleanup ack failed")

    with monkeypatch.context() as patch:
        if fault.startswith("abandon"):
            patch.setattr(env.documents, "abandon_revision", bad_abandon)
        elif fault.startswith("purge"):
            patch.setattr(env.documents, "purge_inactive_revision", bad_purge)
        else:
            patch.setattr(env.ctx, "put_bounded", bad_ack)
        with pytest.raises(ConnectionError):
            await env.stages().cleanup(env.ctx)
    saved = await env.read_tasks.require(env.ctx.task_id)
    assert saved.context["revision_publication"]["phase"] == (
        "cleanup_complete" if fault == "ack-after" else "cleanup_pending"
    )
    await _assert_visible(env.service, winner)
    calls = env.model.calls
    fresh, stages = env.fresh_context(), env.stages(independent=True)
    assert (await stages.activate(fresh)).superseded
    await stages.cleanup(fresh)
    manifest = await env.read_documents.reserve_revision(
        IndexHandoff.from_plan(env.plan, task_id=env.ctx.task_id).draft
    )
    assert manifest.status is RevisionStatus.PURGED and manifest.generation == 1
    assert (await env.read_tasks.require(env.ctx.task_id)).context[
        "revision_publication"
    ]["phase"] == "cleanup_complete"
    assert (
        await env.reader.count(
            env.plan.key.kb_id,
            filter={
                "source_id": "source",
                "document_revision": env.plan.key.revision_id,
            },
        )
        == 0
    )
    assert env.model.calls == calls
    await _assert_visible(env.service, winner)


async def test_real_milvus_accepts_late_upsert_after_purge_and_durable_intent_compensates(
    publication_env: Environment,
) -> None:
    env = publication_env
    winner = await env.replace()
    await env.stages().activate(env.ctx)
    accepted, commit = asyncio.Event(), asyncio.Event()
    child = env.plan.vector_children[0]

    async def delayed_write() -> None:
        # 故障边界模拟已接受但尚未执行的旧请求；最终落库用真实独立 Milvus 客户端。
        accepted.set()
        await commit.wait()
        await env.writer.upsert(
            env.plan.key.kb_id,
            [VectorRecord(child.id, child.text, [1.0, 0.0], dict(child.metadata))],
        )

    remote = asyncio.create_task(delayed_write())
    try:
        await accepted.wait()
        await env.stages().cleanup(env.ctx)
        commit.set()
        await remote
        condition = {
            "source_id": "source",
            "document_revision": env.plan.key.revision_id,
        }
        assert await env.reader.count(env.plan.key.kb_id, filter=condition) == 1
        collector = RevisionGarbageCollector(
            document_store=env.read_documents, vector_store=env.reader
        )
        assert not (await collector.sweep()).completed
        assert await env.reader.count(env.plan.key.kb_id, filter=condition) == 1
        await _assert_visible(env.service, winner)
        # 新连接只读 Task 中的 complete 意图；不能依赖内存中的 ActivationOutcome。
        await env.stages(independent=True).cleanup(env.fresh_context())
        assert await env.reader.count(env.plan.key.kb_id, filter=condition) == 0
        assert (await env.read_tasks.require(env.ctx.task_id)).context[
            "revision_publication"
        ]["phase"] == "cleanup_complete"
        await _assert_visible(env.service, winner)
    finally:
        commit.set()
        await remote
