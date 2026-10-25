from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from comet_rag.api.main import _install_exception_handlers
from comet_rag.api.routes import admin, ingest, search, tasks
from comet_rag.composition.bootstrap import build_context
from comet_rag.composition.revision_preflight import inspect_revision_deployment
from comet_rag.config.schemas import APPConfig
from comet_rag.infrastructure.task_execution.arq import LANE_QUEUES, ArqExecutor
from comet_rag.services.knowledge_base import KnowledgeBaseSpec
from comet_rag.tasks import TaskStatus
from comet_rag.workers.embedder import PROFILE as IO
from comet_rag.workers.preprocessor import PROFILE as CPU
from tests.integration.test_revision_ingestion_worker import (
    Production,
    document,
)
from tests.integration.test_revision_ingestion_worker import (
    production as target_production,  # noqa: F401 —— 显式复用已验证的目标部署夹具
)
from tests.integration.test_revision_lifecycle import _Embedding

pytestmark = pytest.mark.integration


def migrate(dsn):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture
async def deployment_pair(target_production):  # noqa: F811 —— pytest 注入导入的夹具
    """目标沿用固定 Milvus 测试库/随机前缀；旧端另建 SQL DB、Redis DB 与前缀。"""
    target = target_production
    raw = target.config.model_dump(mode="python")
    old_name = "ct_rehearsal_" + uuid4().hex
    raw["infrastructure_config"]["database"]["database"] = old_name
    raw["infrastructure_config"]["redis"]["db_index"] = 11
    raw["infrastructure_config"]["vector_database"]["collection_prefix"] = (
        "ctlegacy_" + uuid4().hex[:12]
    )
    raw["revision_ingestion"]["enabled"] = False
    raw["revision_reconciliation"]["enabled"] = False
    raw["retrieval"]["revision_aware"] = False
    legacy_config = APPConfig.model_validate(raw)
    assert legacy_config.infrastructure_config.database is not None
    admin_engine = create_async_engine(
        target.config.infrastructure_config.database.dsn, isolation_level="AUTOCOMMIT"
    )
    created = False
    try:
        async with admin_engine.connect() as connection:
            # 名称仅由固定前缀和 uuid 生成，绝不接收配置/用户标识作为 SQL 标识符。
            await connection.execute(text(f'CREATE DATABASE "{old_name}"'))
            created = True
        await asyncio.to_thread(
            migrate, legacy_config.infrastructure_config.database.dsn
        )
        async with AsyncExitStack() as cleanup:
            context = await build_context(
                legacy_config,
                embedding_model=_Embedding(),
                pipeline_config=target.pipeline,
            )
            cleanup.push_async_callback(context.close)
            await context.knowledge_base.create(
                KnowledgeBaseSpec(kb_id="kb-production")
            )
            cleanup.push_async_callback(
                context.vector_store.drop_collection, "kb-production"
            )
            assert isinstance(context.task_executor, ArqExecutor)
            pool = await context.task_executor.pool()
            await pool.delete(*LANE_QUEUES.values())
            cleanup.push_async_callback(pool.delete, *LANE_QUEUES.values())
            app = FastAPI()
            _install_exception_handlers(app)
            for router in (ingest.router, tasks.router, search.router, admin.router):
                app.include_router(router)
            app.state.ctx = context
            client = await cleanup.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://legacy"
                )
            )
            legacy = Production(
                legacy_config, target.pipeline, context, client, target.path, cleanup
            )
            await legacy.start(CPU)
            await legacy.start(IO)
            yield legacy, target
    finally:
        try:
            if created:
                async with admin_engine.connect() as connection:
                    await connection.execute(text(f'DROP DATABASE "{old_name}"'))
        finally:
            await admin_engine.dispose()


async def read(client):
    response = await client.post(
        "/search",
        json={
            "kb_id": "kb-production",
            "query": "量子纠缠",
            "mode": "dense",
            "top_k": 10,
            "rerank": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["chunks"]


async def submit_done(env):
    result = await env.done(await env.submit())
    assert result.status is TaskStatus.SUCCEEDED, result.error
    return result


def pause(env):
    # Context 值是部署完成后的准入状态；这里只模拟单 API，实际部署须逐实例核验。
    env.context.accept_new_ingest_tasks = False
    raw = env.config.model_dump(mode="python")
    raw["ingest_policy"]["accept_new_tasks"] = False
    env.config = APPConfig.model_validate(raw)


@pytest.mark.parametrize(
    "fault", ["none", "rebuild-before-activation", "activation-response"]
)
async def test_real_isolated_rebuild_read_cutover_and_rollback(deployment_pair, fault):
    legacy, target = deployment_pair
    frozen_digest = sha256(target.path.read_bytes()).hexdigest()
    old = await submit_done(legacy)
    old_rows = await read(legacy.client)
    assert old_rows and all("旧版本" in row["text"] for row in old_rows)
    pause(legacy)
    assert (
        await legacy.client.post(
            "/ingest", json={"kb_id": "kb-production", "source": str(legacy.path)}
        )
    ).status_code == 503
    assert inspect_revision_deployment(
        previous=legacy.config,
        api=target.config,
        preprocessor=target.config,
        embedder=target.config,
        pipelines=dict.fromkeys(("api", "preprocessor", "embedder"), target.pipeline),
    ).passed
    # 冻结的生成资料在相同路径重建；没有把旧 ANN 命中当作资料清单。
    assert sha256(target.path.read_bytes()).hexdigest() == frozen_digest
    await target.start(CPU)
    blocked, release = asyncio.Event(), asyncio.Event()
    injected = False

    def inject(context):
        original = context.document_store.activate_revision

        async def activation(key):
            nonlocal injected
            if fault == "rebuild-before-activation" and not injected:
                injected = True
                blocked.set()
                await release.wait()
                raise ValueError("controlled rebuild failed before activation")
            result = await original(key)
            if fault == "activation-response" and not injected:
                injected = True
                raise ConnectionError("activation committed; response lost")
            return result

        context.document_store.activate_revision = activation

    await target.start(IO, fault=inject)
    rebuild_id = await target.submit()
    try:
        if fault == "rebuild-before-activation":
            await asyncio.wait_for(blocked.wait(), 20)
            assert await read(target.client) == []
            assert await read(legacy.client) == old_rows
    finally:
        release.set()
    rebuilt = await target.done(rebuild_id)
    if fault == "rebuild-before-activation":
        assert rebuilt.status is TaskStatus.FAILED
        assert await read(legacy.client) == old_rows
        response = await target.client.post(f"/tasks/{rebuild_id}/retry")
        assert response.status_code == 200, response.text
        rebuilt = await target.done(rebuild_id)
    assert rebuilt.status is TaskStatus.SUCCEEDED, rebuilt.error
    # 人工重试重置本轮尝试计数；自动重试才在同一轮累计。
    assert rebuilt.attempts == (2 if fault == "activation-response" else 1)
    await target.visible(rebuilt.result["revision_id"], text="旧版本")
    assert await legacy.context.task_store.get(rebuild_id) is None
    assert await target.context.task_store.get(old.task_id) is None
    # 切换是测试调用方更换 API 路由，不修改 manifest 或声称项目包含流量控制器。
    routed_client = target.client
    assert all(
        row["metadata"]["document_revision"] == rebuilt.result["revision_id"]
        for row in await read(routed_client)
    )
    pause(target)
    routed_client = legacy.client
    assert await read(routed_client) == old_rows


async def test_real_post_cutover_update_makes_direct_rollback_stale(deployment_pair):
    legacy, target = deployment_pair
    await submit_done(legacy)
    old_rows = await read(legacy.client)
    pause(legacy)
    await target.start(CPU)
    await target.start(IO)
    await submit_done(target)
    document(target.path, "量子纠缠新版本说明。" * 2)
    newer = await submit_done(target)

    async def assert_updated_target():
        rows = await read(target.client)
        assert len(rows) == 2
        assert all(
            "新版本" in row["text"]
            and row["metadata"]["document_revision"] == newer.result["revision_id"]
            for row in rows
        )

    await assert_updated_target()
    pause(target)
    # 反例：旧端不会神奇地跟上新端。只换读地址会丢失可见更新。
    assert await read(legacy.client) == old_rows
    assert all("新版本" not in row["text"] for row in old_rows)
    # 保存且人工确认差异资料后，在隔离旧端重建，再核验；没有复制 revision metadata。
    legacy.context.accept_new_ingest_tasks = True
    await submit_done(legacy)
    pause(legacy)
    recovered = await read(legacy.client)
    # 既有 legacy 全部写完后清旧尾块；差异重建应移除短文更新留下的旧尾块。
    assert len(recovered) == 2
    assert all(
        "新版本" in row["text"] and "旧版本" not in row["text"] for row in recovered
    )
    await assert_updated_target()
