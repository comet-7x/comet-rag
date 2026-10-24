from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from arq.worker import create_worker
from docx import Document
from fastapi import FastAPI

from comet_rag.api.main import _install_exception_handlers
from comet_rag.api.routes import admin, ingest, search, tasks
from comet_rag.composition.bootstrap import build_context
from comet_rag.composition.context import Context
from comet_rag.config.schemas import APPConfig
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.task_execution.arq import LANE_QUEUES, ArqExecutor
from comet_rag.services.knowledge_base import KnowledgeBaseSpec
from comet_rag.services.revision_context import load_task_handoff
from comet_rag.services.revision_ingestion import REVISION_INGEST_KIND
from comet_rag.tasks import TaskStatus
from comet_rag.workers.base import build_settings
from comet_rag.workers.embedder import PROFILE as IO
from comet_rag.workers.preprocessor import PROFILE as CPU
from tests.contracts.support import wait_for_terminal, wait_until
from tests.integration.conftest import truncate_tables
from tests.integration.test_revision_lifecycle import _Embedding
from tests.integration.worker_config import make_worker_config
from tests.integration.worker_lifecycle import stop_workers

pytestmark = pytest.mark.integration


def document(path: Path, text: str) -> None:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


@dataclass(slots=True)
class Production:
    config: APPConfig
    pipeline: PipelineConfig
    context: Context
    client: httpx.AsyncClient
    path: Path
    cleanup: AsyncExitStack

    async def start(self, profile, *, fault=None, config=None):
        settings = build_settings(
            profile,
            config=config or self.config,
            embedding_model=_Embedding(),
            pipeline_config=self.pipeline,
        )
        startup = settings.on_startup

        async def initialize(ctx):
            await startup(ctx)
            if fault is not None:
                fault(ctx["context"])

        settings.on_startup = staticmethod(initialize)
        worker = create_worker(
            settings,
            poll_delay=0.01,
            handle_signals=False,
            keep_result=0,
            log_results=False,
        )
        self.cleanup.push_async_callback(worker.close)
        running = asyncio.create_task(worker.async_run())
        stopped = False

        async def stop():
            nonlocal stopped
            if not stopped:
                await stop_workers([running], [worker])
                stopped = True

        self.cleanup.push_async_callback(stop)

        async def ready():
            if running.done():
                running.result()
            return "context" in worker.ctx

        await wait_until(ready, timeout=8, message="生产 worker 启动失败")
        return worker, stop

    async def submit(self):
        response = await self.client.post(
            "/ingest", json={"kb_id": "kb-production", "source": str(self.path)}
        )
        assert response.status_code == 202, response.text
        return response.json()["task_id"]

    async def done(self, task_id):
        return await wait_for_terminal(self.context.task_store, task_id, timeout=25)

    async def visible(self, revision, *, text="量子纠缠"):
        for mode in ("dense", "keyword", "hybrid"):
            for level, expected in (("child", 4), ("parent", 2)):
                response = await self.client.post(
                    "/search",
                    json={
                        "kb_id": "kb-production",
                        "query": "量子纠缠",
                        "mode": mode,
                        "return_level": level,
                        "top_k": 10,
                        "rerank": False,
                    },
                )
                assert response.status_code == 200, response.text
                chunks = response.json()["chunks"]
                assert len(chunks) == expected
                assert all(
                    row["metadata"]["document_revision"] == revision
                    and text in row["text"]
                    for row in chunks
                )


@pytest.fixture
async def production(
    database: Database, postgres_dsn, redis_url, milvus_uri, milvus_database, tmp_path
):
    await truncate_tables(database, "tasks", "knowledge_bases")
    config = make_worker_config(
        postgres_dsn,
        redis_url,
        redis_db=10,
        dim=2,
        milvus_uri=milvus_uri,
        milvus_database=milvus_database,
        collection_prefix="ctproduction_" + uuid4().hex[:12],
    )
    data = config.model_dump(mode="python")
    data["retrieval"]["revision_aware"] = True
    data["revision_reconciliation"] = {
        "enabled": True,
        "task_kind": REVISION_INGEST_KIND,
    }
    data["revision_ingestion"] = {
        "enabled": True,
        "kb_ids": ["kb-production"],
        "parent_chunk_size": 20,
    }
    config = APPConfig.model_validate(data)
    pipeline = PipelineConfig(
        chunk_size=10, chunk_overlap=0, embed_batch_size=2, max_concurrency=2
    )
    path = tmp_path / "production.docx"
    document(path, "量子纠缠旧版本说明。" * 4)
    try:
        async with AsyncExitStack() as cleanup:
            context = await build_context(
                config, embedding_model=_Embedding(), pipeline_config=pipeline
            )
            cleanup.push_async_callback(context.close)
            await context.knowledge_base.create(
                KnowledgeBaseSpec(kb_id="kb-production")
            )
            cleanup.push_async_callback(
                context.vector_store.drop_collection, "kb-production"
            )
            executor = context.task_executor
            assert isinstance(executor, ArqExecutor)
            pool = await executor.pool()
            await pool.delete(*LANE_QUEUES.values())
            cleanup.push_async_callback(pool.delete, *LANE_QUEUES.values())
            app = FastAPI()
            _install_exception_handlers(app)
            for router in (ingest.router, tasks.router, search.router, admin.router):
                app.include_router(router)
            app.state.ctx = context
            client = await cleanup.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                )
            )
            yield Production(config, pipeline, context, client, path, cleanup)
    finally:
        await truncate_tables(database, "tasks", "knowledge_bases")


@pytest.mark.parametrize("failure", ["none", "ready", "activation"])
async def test_real_cpu_handoff_restarts_on_io_and_recovers_lost_responses(
    production, failure
):
    env = production
    cpu, stop = await env.start(CPU)
    task_id = await env.submit()

    async def handed():
        task = await env.context.task_store.require(task_id)
        return task.status is TaskStatus.PENDING and task.resume_stage == "indexing"

    await wait_until(handed, timeout=15, message="CPU 未持久化父块与移交载荷")
    saved = await env.context.task_store.require(task_id)
    draft = load_task_handoff(saved.context, task_id).draft
    assert draft.parent_count == 2 and draft.child_count == 4
    assert saved.context["text"] is None and saved.context["revision_prepare"] is None
    assert [item.stage for item in saved.stage_history] == ["extracting", "preparing"]
    assert cpu.ctx["context"].embedding_model.calls == 0
    assert await env.context.vector_store.count("kb-production") == 0
    await stop()
    # 新 IO Context 完全依赖数据库：原文件消失也不允许重新提取。
    env.path.unlink()
    injected = False

    def fault(context):
        nonlocal injected
        method = "mark_ready" if failure == "ready" else "activate_revision"
        original = getattr(context.document_store, method)

        async def lost(key):
            nonlocal injected
            result = await original(key)
            if failure != "none" and not injected:
                injected = True
                raise ConnectionError("committed response lost")
            return result

        setattr(context.document_store, method, lost)

    io, _ = await env.start(IO, fault=fault)
    done = await env.done(task_id)
    assert done.status is TaskStatus.SUCCEEDED, done.error
    assert done.attempts == (1 if failure == "none" else 2)
    assert injected is (failure != "none")
    assert io.ctx["context"].embedding_model.calls == 4
    assert (
        done.result["revision_id"] == draft.key.revision_id
        and not done.result["superseded"]
    )
    manifest = await env.context.document_store.reserve_revision(draft)
    assert manifest.generation == 1 and manifest.status.value == "active"
    await env.visible(draft.key.revision_id, text="旧版本")


async def test_real_superseded_cleanup_retries_without_overwriting_new_active(
    production,
):
    env = production
    await env.start(CPU)
    blocked, release = asyncio.Event(), asyncio.Event()
    old_revision = None
    failed_delete = False

    def inject(context):
        activate = context.document_store.activate_revision
        delete = context.vector_store.delete

        async def delayed(key):
            nonlocal old_revision
            if old_revision is None:
                old_revision = key.revision_id
                blocked.set()
                await release.wait()
            return await activate(key)

        async def lost(*args, **kwargs):
            nonlocal failed_delete
            await delete(*args, **kwargs)
            if not failed_delete:
                failed_delete = True
                raise ConnectionError("superseded deletion response lost")

        context.document_store.activate_revision = delayed
        context.vector_store.delete = lost

    io, _ = await env.start(IO, fault=inject)
    old_id = await env.submit()
    try:
        await asyncio.wait_for(blocked.wait(), 15)
        document(env.path, "量子纠缠新版本说明。" * 4)
        new_id = await env.submit()
        newer = await env.done(new_id)
        assert newer.status is TaskStatus.SUCCEEDED and not newer.result["superseded"]
        await env.visible(newer.result["revision_id"], text="新版本")
    finally:
        release.set()
    older = await env.done(old_id)
    assert older.status is TaskStatus.SUCCEEDED and older.result["superseded"] is True
    assert older.attempts == 2 and failed_delete
    assert io.ctx["context"].embedding_model.calls == 8
    assert [item.stage for item in older.stage_history].count("cleanup") == 2
    assert [item.stage for item in older.stage_history].count("indexing") == 1
    draft = load_task_handoff(older.context, old_id).draft
    assert (
        await env.context.document_store.reserve_revision(draft)
    ).status.value == "purged"
    await env.visible(newer.result["revision_id"], text="新版本")


async def test_real_active_publication_keeps_result_while_previous_gc_retries(
    production,
):
    env = production
    await env.start(CPU)
    io, _ = await env.start(IO)
    old = await env.done(await env.submit())
    assert old.status is TaskStatus.SUCCEEDED
    context = io.ctx["context"]
    delete = context.vector_store.delete
    injected = False

    async def lost(*args, **kwargs):
        nonlocal injected
        await delete(*args, **kwargs)
        if not injected:
            injected = True
            raise ConnectionError("previous GC acknowledgment lost")

    context.vector_store.delete = lost
    document(env.path, "量子纠缠新版本说明。" * 4)
    newer = await env.done(await env.submit())
    assert newer.status is TaskStatus.SUCCEEDED and newer.attempts == 2
    assert not newer.result["superseded"] and injected
    assert context.embedding_model.calls == 8
    assert [item.stage for item in newer.stage_history].count("activating") == 1
    assert [item.stage for item in newer.stage_history].count("cleanup") == 2
    old_draft = load_task_handoff(old.context, old.task_id).draft
    assert (
        await env.context.document_store.reserve_revision(old_draft)
    ).status.value == "purged"
    await env.visible(newer.result["revision_id"], text="新版本")


async def test_real_mismatched_io_plan_is_rejected_before_embedding(production):
    env = production
    _, stop = await env.start(CPU)
    task_id = await env.submit()

    async def handed():
        task = await env.context.task_store.require(task_id)
        return task.status is TaskStatus.PENDING and task.resume_stage == "indexing"

    await wait_until(handed, timeout=15, message="CPU 未移交")
    await stop()
    data = env.config.model_dump(mode="python")
    data["revision_ingestion"]["parent_chunk_size"] = 30
    io, _ = await env.start(IO, config=APPConfig.model_validate(data))
    result = await env.done(task_id)
    assert result.status is TaskStatus.FAILED and result.attempts == 1
    assert io.ctx["context"].embedding_model.calls == 0
    assert await env.context.vector_store.count("kb-production") == 0
    assert "revision_publication" not in result.context
