from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from comet_rag.composition import bootstrap
from comet_rag.composition.revision_config import revision_binding
from comet_rag.config.schemas import APPConfig
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.services.revision_ingestion import (
    REVISION_INGEST_KIND,
    RevisionIngestRunner,
)
from comet_rag.tasks import TaskStatus
from tests.unit.test_ingest_pause import application
from tests.unit.test_revision_bootstrap import Embedding
from tests.unit.test_revision_maintenance_wiring import (
    configuration,
    fake_storage_builders,
)


def writing_config():
    data = configuration().model_dump(mode="python")
    data["retrieval"]["revision_aware"] = True
    data["revision_reconciliation"]["task_kind"] = REVISION_INGEST_KIND
    data["revision_ingestion"] = {"enabled": True, "kb_ids": ["new-kb"]}
    return APPConfig.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("kb_ids", []),
        ("kb_ids", [" "]),
        ("kb_ids", ["same", "same"]),
        ("kb_ids", ["x" * 129]),
        ("parent_chunk_size", True),
        ("parent_chunk_size", 0),
    ],
)
def test_invalid_write_config_fails_before_resource_creation(field, value):
    data = writing_config().model_dump(mode="python")
    data["revision_ingestion"][field] = value
    with pytest.raises(ValidationError):
        APPConfig.model_validate(data)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("retrieval", "revision_aware", False),
        ("revision_reconciliation", "enabled", False),
        ("revision_reconciliation", "task_kind", "ingest"),
        ("backends", "task_executor", "inprocess"),
    ],
)
def test_write_requires_paired_reads_and_durable_maintenance(section, field, value):
    data = writing_config().model_dump(mode="python")
    data[section][field] = value
    with pytest.raises(ValidationError):
        APPConfig.model_validate(data)


@pytest.mark.parametrize(
    "changed", ["model", "dimension", "prefix", "redis", "kb", "chunking"]
)
def test_binding_changes_with_every_write_relevant_scope(changed):
    first = writing_config()
    data = first.model_dump(mode="python")
    pipeline = PipelineConfig()
    other_pipeline = pipeline.model_copy(deep=True)
    infra = data["infrastructure_config"]
    if changed == "model":
        infra["embedding_model"]["model_name"] = "other"
    elif changed == "dimension":
        infra["embedding_model"]["dim"] += 1
    elif changed == "prefix":
        infra["vector_database"]["collection_prefix"] = "new_prefix_"
    elif changed == "redis":
        infra["redis"]["db_index"] += 1
    elif changed == "kb":
        data["revision_ingestion"]["kb_ids"] = ["other-kb"]
    else:
        other_pipeline.chunk_size += 1
    assert revision_binding(first, pipeline) != revision_binding(
        APPConfig.model_validate(data), other_pipeline
    )


def test_credentials_do_not_change_namespace_identity_or_get_persisted():
    first = writing_config()
    data = first.model_dump(mode="python")
    data["infrastructure_config"]["database"]["password"] = "different-secret"  # noqa: S105 — 仅验证凭据轮换不改变身份
    assert revision_binding(first, PipelineConfig()) == revision_binding(
        APPConfig.model_validate(data), PipelineConfig()
    )


async def test_oversized_child_plan_is_rejected_before_opening_resources(monkeypatch):
    builder = AsyncMock()
    monkeypatch.setattr(bootstrap, "build_database", builder)
    data = writing_config().model_dump(mode="python")
    data["revision_ingestion"]["parent_chunk_size"] = 1
    with pytest.raises(ValueError, match="parent_chunk_size"):
        await bootstrap.build_context(
            APPConfig.model_validate(data), embedding_model=Embedding()
        )
    builder.assert_not_called()


async def test_new_mode_registers_only_revision_writer_and_preserves_maintenance(
    monkeypatch,
):
    _, store, _ = fake_storage_builders(monkeypatch)
    context = await bootstrap.build_context(
        writing_config(),
        embedding_model=Embedding(),
        kb_repository=InMemoryKnowledgeBaseRepository(),
    )
    try:
        assert context.ingest_kind == REVISION_INGEST_KIND
        assert context.runners.kinds() == [REVISION_INGEST_KIND]
        assert isinstance(
            context.runners.get(REVISION_INGEST_KIND), RevisionIngestRunner
        )
        assert context.revision_ingest_binding is not None
        assert context.revision_maintenance is not None
        assert context.retrieval._document_store is context.document_store
        assert (await context.task_service.backlog())["pending"] == 0
        assert len(await store.list_tasks()) == 1
    finally:
        await context.close()


async def test_legacy_task_namespace_is_rejected_and_owned_resources_are_closed(
    monkeypatch,
):
    database, store, vectors = fake_storage_builders(monkeypatch)
    await store.create("ingest")
    close = AsyncMock(wraps=vectors.close)
    monkeypatch.setattr(vectors, "close", close)
    with pytest.raises(ValueError, match="legacy ingest"):
        await bootstrap.build_context(
            writing_config(),
            embedding_model=Embedding(),
            kb_repository=InMemoryKnowledgeBaseRepository(),
        )
    database.close.assert_awaited_once()
    close.assert_awaited_once()


async def test_api_chooses_bound_kind_and_rejects_outside_kb_before_source_io():
    app, store, executor, policy = application(accepting=True)
    app.state.ctx.ingest_kind = REVISION_INGEST_KIND
    app.state.ctx.revision_ingest_binding = "scope"
    app.state.ctx.revision_ingest_kb_ids = frozenset({"new-kb"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rejected = await client.post(
            "/ingest", json={"kb_id": "old-kb", "source": "https://example.test/file"}
        )
        assert rejected.status_code == 400
        policy.check.assert_not_called()
        response = await client.post(
            "/ingest", json={"kb_id": "new-kb", "source": "https://example.test/file"}
        )
    assert response.status_code == 202
    [saved] = await store.list_tasks()
    assert (
        saved.kind == REVISION_INGEST_KIND
        and saved.request.get("revision_binding") == "scope"
    )
    assert executor.submitted == [saved.task_id]


async def test_retry_cannot_change_ingest_modes_and_pause_covers_revision_kind():
    app, store, executor, _ = application(accepting=True)
    source = await store.create(REVISION_INGEST_KIND)
    await store.transition(source.task_id, TaskStatus.FAILED)
    before = await store.require(source.task_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(f"/tasks/{source.task_id}/retry")).status_code == 409
        app.state.ctx.ingest_kind = REVISION_INGEST_KIND
        app.state.ctx.accept_new_ingest_tasks = False
        assert (await client.post(f"/tasks/{source.task_id}/retry")).status_code == 503
    assert await store.require(source.task_id) == before and executor.submitted == []


async def test_retry_rejects_changed_binding_without_reopening_the_task():
    app, store, executor, _ = application(accepting=True)
    app.state.ctx.ingest_kind = REVISION_INGEST_KIND
    app.state.ctx.revision_ingest_binding = "new-config"
    app.state.ctx.revision_ingest_kb_ids = frozenset({"new-kb"})
    source = await store.create(
        REVISION_INGEST_KIND,
        request={
            "kb_id": "new-kb",
            "source": "fixture",
            "revision_binding": "old-config",
        },
    )
    await store.transition(source.task_id, TaskStatus.FAILED)
    before = await store.require(source.task_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(f"/tasks/{source.task_id}/retry")).status_code == 409
    assert await store.require(source.task_id) == before and executor.submitted == []
