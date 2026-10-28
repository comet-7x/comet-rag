from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from comet_rag.composition import bootstrap
from comet_rag.composition.mineru import MinerUIntegration
from comet_rag.composition.revision_config import revision_binding
from comet_rag.config.schemas import APPConfig, MinerUConfig
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.services.evidence_publication import (
    PERMISSION,
    PUBLICATION_PERMISSION_KIND,
)
from comet_rag.services.revision_ingestion import (
    REVISION_INGEST_KIND,
    RevisionIngestRunner,
)
from tests.unit.services.test_mineru_capture import (
    capture_environment as capture_environment,
)
from tests.unit.test_bootstrap import make_config
from tests.unit.test_revision_bootstrap import Embedding
from tests.unit.test_revision_ingestion_wiring import writing_config
from tests.unit.test_revision_maintenance_wiring import fake_storage_builders


async def test_opt_in_workflow_reaches_extraction_and_parent_planning(
    monkeypatch, capture_environment
):
    env = capture_environment
    fake_storage_builders(monkeypatch)
    settings = writing_config().model_dump(mode="python")
    settings["revision_ingestion"]["kb_ids"] = ["kb"]
    config = APPConfig.model_validate(settings)
    context = await bootstrap.build_context(
        config,
        embedding_model=Embedding(),
        kb_repository=InMemoryKnowledgeBaseRepository(),
        mineru_workflow=env.workflow,
    )
    try:
        runner = context.runners.get(REVISION_INGEST_KIND)
        assert isinstance(runner, RevisionIngestRunner)
        assert runner._mineru is env.workflow
        assert runner._extraction._mineru is env.workflow
        assert context.revision_ingest_binding != revision_binding(
            config, PipelineConfig()
        )
        assert env.server.posts == 0  # 启动只做持久化条件发布验收，不提交模型任务。
    finally:
        await context.close()


@pytest.mark.parametrize("fault", ["legacy_mode", "no_parents", "two_protocols"])
async def test_invalid_workflow_wiring_rejects_before_building_resources(
    monkeypatch, capture_environment, fault
):
    builder = AsyncMock()
    monkeypatch.setattr(bootstrap, "build_database", builder)
    config = make_config() if fault == "legacy_mode" else writing_config()
    if fault == "no_parents":
        config.revision_ingestion.parent_chunk_size = None
    elif fault == "two_protocols":
        config.infrastructure_config.mineru = MinerUConfig(
            enabled=True, base_url="https://old.test"
        )
    with pytest.raises(ValueError, match="(接线|协议)"):
        await bootstrap.build_context(
            config, mineru_workflow=capture_environment.workflow
        )
    builder.assert_not_called()


@pytest.mark.parametrize("failure", ["factory", "permission", "runner"])
async def test_factory_failure_rolls_back_every_registered_resource_and_preserves_error(
    monkeypatch, capture_environment, failure
):
    _, tasks, _ = fake_storage_builders(monkeypatch)
    order = []

    async def close(name, broken=False):
        order.append(name)
        if broken:
            raise OSError("secondary close failure")

    async def factory(config, store, documents, resources):
        assert asyncio.get_running_loop() is loop
        assert store is tasks and documents is not None
        resources.push_async_callback(close, "client")
        resources.push_async_callback(close, "producer", True)
        if failure == "factory":
            raise ValueError("primary startup failure")
        return MinerUIntegration(capture_environment.workflow)

    if failure == "permission":
        monkeypatch.setattr(
            capture_environment.workflow,
            "prepare_publication",
            AsyncMock(side_effect=ValueError("primary startup failure")),
        )
    elif failure == "runner":
        monkeypatch.setattr(
            bootstrap,
            "wire_runners",
            lambda *a, **k: (_ for _ in ()).throw(
                ValueError("primary startup failure")
            ),
        )
    loop = asyncio.get_running_loop()
    with pytest.raises(ValueError, match="primary startup failure"):
        await bootstrap.build_context(
            writing_config(), embedding_model=Embedding(), mineru_factory=factory
        )
    assert order == ["producer", "client"]


@pytest.mark.parametrize("cancel_during", ["startup", "shutdown"])
async def test_factory_resources_drain_before_repeated_cancellation_propagates(
    monkeypatch, capture_environment, cancel_during
):
    fake_storage_builders(monkeypatch)
    context = None
    entered, release, factory_entered = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    order = []

    async def close():
        entered.set()
        await release.wait()
        order.append("closed")

    async def factory(config, tasks, documents, resources):
        resources.push_async_callback(close)
        factory_entered.set()
        if cancel_during == "startup":
            await asyncio.Event().wait()
        return MinerUIntegration(capture_environment.workflow)

    building = asyncio.create_task(
        bootstrap.build_context(
            writing_config(), embedding_model=Embedding(), mineru_factory=factory
        )
    )
    await factory_entered.wait()
    if cancel_during == "startup":
        operation = building
        operation.cancel()
    else:
        context = await building
        operation = asyncio.create_task(context.close())
    await entered.wait()
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done() and order == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert order == ["closed"]
    if cancel_during == "shutdown":
        assert context is not None
        await context.close()
        assert order == ["closed"]


async def test_factory_success_is_owned_until_executor_drains_then_closes_in_reverse(
    monkeypatch, capture_environment
):
    database, tasks, _ = fake_storage_builders(monkeypatch)
    order = []

    async def close(name):
        order.append(name)

    database.close = AsyncMock(side_effect=lambda: order.append("database"))

    async def factory(config, store, documents, resources):
        resources.push_async_callback(close, "http")
        resources.push_async_callback(close, "s3")
        return MinerUIntegration(capture_environment.workflow)

    context = await bootstrap.build_context(
        writing_config(), embedding_model=Embedding(), mineru_factory=factory
    )
    (permission,) = await tasks.list_tasks(kind=PUBLICATION_PERMISSION_KIND)
    assert permission.context == {PERMISSION: {"state": "verified"}}
    assert order == [] and capture_environment.server.posts == 0
    shutdown = context.task_executor.shutdown

    async def drain():
        order.append("executor")
        await shutdown()

    monkeypatch.setattr(context.task_executor, "shutdown", drain)
    await asyncio.gather(context.close(), context.close())
    assert order == ["executor", "s3", "http", "database"]


async def test_factory_cannot_share_ownership_with_injected_workflow(
    monkeypatch, capture_environment
):
    factory = AsyncMock()
    with pytest.raises(ValueError, match="混用"):
        await bootstrap.build_context(
            writing_config(),
            mineru_workflow=capture_environment.workflow,
            mineru_factory=factory,
        )
    factory.assert_not_called()


@pytest.mark.parametrize("entry", ["api", "cpu", "io"])
async def test_process_entry_builds_and_releases_factory_on_its_own_loop(
    monkeypatch, capture_environment, entry
):
    from comet_rag.api.main import create_app  # noqa: PLC0415
    from comet_rag.workers.base import build_settings  # noqa: PLC0415
    from comet_rag.workers.embedder import PROFILE as IO  # noqa: PLC0415
    from comet_rag.workers.preprocessor import PROFILE as CPU  # noqa: PLC0415

    fake_storage_builders(monkeypatch)
    order = []
    loop = asyncio.get_running_loop()

    async def close():
        assert asyncio.get_running_loop() is loop
        order.append("closed")

    async def factory(config, store, documents, resources):
        assert asyncio.get_running_loop() is loop
        order.append("opened")
        resources.push_async_callback(close)
        return MinerUIntegration(capture_environment.workflow)

    config = writing_config()
    if entry == "api":
        app = create_app(config, embedding_model=Embedding(), mineru_factory=factory)
        async with app.router.lifespan_context(app):
            assert app.state.ctx.mineru_workflow is capture_environment.workflow
            assert order == ["opened"]
        assert not hasattr(app.state, "ctx")
    else:
        settings = build_settings(
            CPU if entry == "cpu" else IO,
            config=config,
            embedding_model=Embedding(),
            mineru_factory=factory,
        )
        state = dict(settings.ctx)
        await settings.on_startup(state)
        assert state["context"].mineru_workflow is capture_environment.workflow
        assert order == ["opened"]
        await settings.on_shutdown(state)
        assert "context" not in state and "executor" not in state
    assert order == ["opened", "closed"]
