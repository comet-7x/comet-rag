from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from arq.worker import create_worker
from fastapi import FastAPI

from comet_rag.api.main import _install_exception_handlers
from comet_rag.api.routes import admin, ingest, tasks
from comet_rag.composition.bootstrap import build_context
from comet_rag.config.schemas import APPConfig
from comet_rag.engines.chunkers import ChunkDraft, FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.vector_store.milvus import AsyncMilvusStore
from comet_rag.infrastructure.task_execution.arq import LANE_QUEUES, ArqExecutor
from comet_rag.ports import NormalizedDocument, VectorRecord
from comet_rag.services.chunking import dump_chunk_drafts
from comet_rag.services.indexing import IndexHandoff, IndexPlanner, RevisionStager
from comet_rag.services.retrieval import RetrievalService
from comet_rag.services.revision_context import get_revision_key
from comet_rag.services.revision_maintenance import RevisionMaintenance
from comet_rag.services.revision_reconciliation import (
    RevisionTaskRetained,
    guard_revision_task_deletion,
)
from comet_rag.tasks import TaskContext, TaskService, TaskStatus
from comet_rag.workers.base import build_settings
from comet_rag.workers.embedder import PROFILE
from tests.contracts.knowledge_base import make_kb
from tests.contracts.support import wait_for_terminal
from tests.integration.test_revision_lifecycle import _assert_visible, _Embedding
from tests.integration.test_revision_publication_recovery import Environment
from tests.integration.test_revision_publication_recovery import (
    publication_env as publication_env,
)
from tests.integration.test_revision_reconciliation_recovery import (
    finish_source,
    operation,
)
from tests.integration.worker_config import make_worker_config
from tests.integration.worker_lifecycle import stop_workers

pytestmark = pytest.mark.integration


async def monitor(env, *, independent=False, page_size=1):
    service = RevisionMaintenance(
        task_store=env.read_tasks if independent else env.tasks,
        reconciler=operation(env, page_size=page_size),
        task_kind="revision-publication-test",
        resource_identity=env.prefix,
    )
    await service.initialize()
    return service


@pytest.mark.parametrize("damaged", [False, True])
async def test_real_missing_history_is_preserved_and_visible_to_a_restarted_monitor(
    publication_env: Environment, damaged, monkeypatch
):
    env = publication_env
    winner = await env.replace()
    await env.tasks.transition(env.ctx.task_id, TaskStatus.CANCELLING)
    await env.tasks.transition(env.ctx.task_id, TaskStatus.CANCELLED)
    # 模拟旧协议在外部写入后遗失 handoff 的持久化记录；真实 manifest/向量仍存在。
    before = await env.read_tasks.require(env.ctx.task_id)
    if damaged:
        context = {
            "revision_key": before.context["revision_key"],
            "revision_index": {"version": True},
        }
    else:
        context = {
            "revision_key": before.context["revision_key"],
            "text": "historical source",
        }
    await env.tasks.update(env.ctx.task_id, context=context)
    source = await env.read_tasks.require(env.ctx.task_id)
    child = env.plan.vector_children[0]
    await env.writer.upsert(
        env.plan.key.kb_id,
        [VectorRecord(child.id, child.text, [1.0, 0.0], dict(child.metadata))],
    )
    first = await monitor(env)
    reserve, delete = (
        AsyncMock(wraps=env.read_documents.reserve_revision),
        AsyncMock(wraps=env.reader.delete),
    )
    monkeypatch.setattr(env.read_documents, "reserve_revision", reserve)
    monkeypatch.setattr(env.reader, "delete", delete)
    report = await first.run_once()
    assert report is not None and report.scanned == 1
    assert report.failed == ((source.task_id,) if damaged else ())
    assert report.missing_payload == (() if damaged else (source.task_id,))
    assert report.deferred == report.cleaned == ()
    reserve.assert_not_awaited()
    delete.assert_not_awaited()
    restarted = await monitor(env, independent=True)
    status = await restarted.status()
    assert status["task_id"] == (await first.status())["task_id"]
    page = status["last_page"]
    assert isinstance(page, dict)
    assert page["missing_payload"] == ([] if damaged else [source.task_id])
    tail = await restarted.run_once()
    assert tail is not None and tail.cycle_complete
    again = await restarted.run_once()
    assert again is not None and again.missing_payload == report.missing_payload
    assert await env.read_tasks.require(source.task_id) == source
    condition = {"source_id": "source", "document_revision": env.plan.key.revision_id}
    assert await env.reader.count(env.plan.key.kb_id, filter=condition) == len(
        env.plan.vector_children
    )
    assert len(
        await env.read_documents.get_parents(
            env.plan.key.kb_id, [p.id for p in env.plan.parents]
        )
    ) == len(env.plan.parents)
    # 底层保留守卫仍然生效；没有把 force 当作恢复或迁移。
    service = TaskService(
        env.read_tasks, AsyncMock(), delete_guard=guard_revision_task_deletion
    )
    with pytest.raises(RevisionTaskRetained):
        await service.delete(source.task_id, force=True)
    await _assert_visible(env.service, winner)


async def test_real_new_kb_and_vector_namespace_isolate_late_legacy_writes(
    publication_env: Environment, database, milvus_uri, milvus_database
):
    env = publication_env
    winner = await finish_source(env)
    old_kb = env.plan.key.kb_id
    new_kb = "kb-rebuilt-" + uuid4().hex[:10]
    prefix = "ctrebuilt_" + uuid4().hex[:12]
    new_vectors = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    try:
        await PostgresKnowledgeBaseRepository(database).create(
            make_kb(new_kb, embedding_dim=2)
        )
        task = await env.tasks.create("revision-migration-test")
        await env.tasks.transition(
            task.task_id, TaskStatus.RUNNING, worker_id="rebuild", attempts=1
        )
        ctx = TaskContext(env.tasks, task.task_id, worker_id="rebuild", attempts=1)
        key = await get_revision_key(ctx, kb_id=new_kb, source_id="source")
        hierarchy = HierarchyBuilder(FixedSizeChunker(20), FixedSizeChunker(10)).split(
            NormalizedDocument(markdown="量子纠缠迁移新说明。" * 2)
        )
        plan = IndexPlanner().plan(
            hierarchy, key=key, source="fixture.md", file_type="md"
        )
        stager = RevisionStager(
            document_store=env.documents,
            vector_store=new_vectors,
            embedding_model=env.model,
        )
        await stager.stage(plan, embedding_dim=2)
        await env.documents.activate_revision(key)
        reader = RetrievalService(
            embedding_model=env.model,
            vector_store=new_vectors,
            keyword_search=new_vectors,
            document_store=env.read_documents,
        )
        # 空计数不是旧端排干证明：之后再实际送入一个无 revision 的 flat 写。
        assert await env.reader.count(old_kb, filter={"source_id": "late-flat"}) == 0
        await env.writer.upsert(
            old_kb,
            [
                VectorRecord(
                    "late-flat",
                    "量子纠缠迟到旧写",
                    [1.0, 0.0],
                    {"source_id": "late-flat"},
                )
            ],
        )
        assert await env.reader.count(old_kb, filter={"source_id": "late-flat"}) == 1
        assert await new_vectors.count(new_kb, filter={"source_id": "late-flat"}) == 0
        await _assert_visible(reader, plan)
        assert await env.read_documents.get_active_revisions(old_kb, ["source"]) == {
            "source": winner.key.revision_id
        }
        assert await env.read_documents.get_active_revisions(new_kb, ["source"]) == {
            "source": key.revision_id
        }
        # 旧端只按原 key 补偿，不能删掉新 KB 的 active 和父块。
        await operation(env).reconcile(await env.read_tasks.require(env.ctx.task_id))
        await _assert_visible(reader, plan)
        assert (
            await env.read_documents.reserve_revision(
                IndexHandoff.from_plan(plan).draft
            )
        ).generation == 1
    finally:
        try:
            await new_vectors.drop_collection(new_kb)
        finally:
            await new_vectors.close()


async def test_real_api_pause_blocks_submission_and_retry_while_existing_io_work_drains(
    publication_env: Environment, postgres_dsn, redis_url, milvus_uri, milvus_database
):
    env = publication_env
    raw = make_worker_config(
        postgres_dsn,
        redis_url,
        redis_db=10,
        dim=2,
        milvus_uri=milvus_uri,
        milvus_database=milvus_database,
        collection_prefix=env.prefix,
    ).model_dump(mode="python")
    raw["infrastructure_config"]["embedding_model"]["model_name"] = (
        "Qwen/Qwen3-VL-Embedding-8B"
    )
    raw["ingest_policy"]["accept_new_tasks"] = False
    config = APPConfig.model_validate(raw)
    async with AsyncExitStack() as cleanup:
        producer = await build_context(config, embedding_model=_Embedding())
        cleanup.push_async_callback(producer.close)
        executor = producer.task_executor
        assert isinstance(executor, ArqExecutor)
        pool = await executor.pool()
        await pool.delete(*LANE_QUEUES.values())
        cleanup.push_async_callback(pool.delete, *LANE_QUEUES.values())
        # 旧部署已经受理并持久化的 IO 续跑点；暂停不会撤销该任务或丢弃队列。
        queued = await env.tasks.create(
            "ingest",
            request={"kb_id": env.plan.key.kb_id, "source": "fixture.md"},
            resume_stage="indexing",
            context={
                "source_id": "accepted-before-pause",
                "source": "fixture.md",
                "file_type": "md",
                "chunk_drafts": dump_chunk_drafts(
                    [ChunkDraft("量子纠缠已受理任务", 0, None, None)]
                ),
            },
        )
        failed = await env.tasks.create(
            "ingest",
            request=queued.request,
            context={"revision_key": {"retained": True}},
        )
        await env.tasks.transition(failed.task_id, TaskStatus.FAILED)
        failed_before = await env.read_tasks.require(failed.task_id)
        app = FastAPI()
        _install_exception_handlers(app)
        for router in (ingest.router, tasks.router, admin.router):
            app.include_router(router)
        app.state.ctx = producer
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            rejected = await client.post(
                "/ingest", json={"kb_id": env.plan.key.kb_id, "source": "fixture.md"}
            )
            retried = await client.post(
                f"/tasks/{failed.task_id}/retry", params={"from_scratch": True}
            )
            assert rejected.status_code == retried.status_code == 503
            assert (await client.get("/admin/limits")).json()[
                "accept_new_ingest_tasks"
            ] is False
        assert await env.read_tasks.require(failed.task_id) == failed_before
        await executor.submit(queued.task_id)
        settings = build_settings(PROFILE, config=config, embedding_model=_Embedding())
        worker = create_worker(
            settings,
            poll_delay=0.01,
            handle_signals=False,
            keep_result=0,
            log_results=False,
        )
        cleanup.push_async_callback(worker.close)
        running = asyncio.create_task(worker.async_run())
        cleanup.push_async_callback(stop_workers, [running], [worker])
        result = await wait_for_terminal(env.read_tasks, queued.task_id, timeout=20)
        assert result.status is TaskStatus.SUCCEEDED
        assert worker.ctx["context"].accept_new_ingest_tasks is False
        assert (
            await env.reader.count(
                env.plan.key.kb_id, filter={"source_id": "accepted-before-pause"}
            )
            == 1
        )
