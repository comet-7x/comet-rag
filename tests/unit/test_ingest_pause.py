from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from comet_rag.api.main import _install_exception_handlers
from comet_rag.api.routes import admin, ingest, tasks
from comet_rag.composition.bootstrap import build_context
from comet_rag.config.schemas import IngestPolicyConfig
from comet_rag.tasks import InMemoryTaskStore, TaskService, TaskStatus
from tests.unit.test_ingest_admission_lifecycle import RecordingExecutor
from tests.unit.test_revision_bootstrap import Embedding, config


def application(*, accepting=False):
    store, executor = InMemoryTaskStore(), RecordingExecutor()
    app = FastAPI()
    _install_exception_handlers(app)
    app.include_router(ingest.router)
    app.include_router(tasks.router)
    app.include_router(admin.router)
    policy = Mock()
    app.state.ctx = SimpleNamespace(
        task_service=TaskService(store, executor),
        accept_new_ingest_tasks=accepting,
        ingest_kind="ingest",
        revision_ingest_binding=None,
        mineru_workflow=None,
        source_policy=policy,
        degradation=None,
    )
    return app, store, executor, policy


async def test_pause_rejects_before_source_resolution_or_task_creation():
    app, store, executor, policy = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ingest",
            json={"kb_id": "kb", "source": "https://document.example/file.docx"},
        )
        status = await client.get("/admin/limits")
        health = await client.get("/admin/health")
    assert response.status_code == 503
    assert response.json()["error"] == "入库准入已暂停，请等待维护完成"
    assert status.json()["accept_new_ingest_tasks"] is False
    assert health.status_code == 200
    policy.check.assert_not_called()
    assert await store.list_tasks() == [] and executor.submitted == []


@pytest.mark.parametrize("from_scratch", [False, True])
async def test_pause_prevents_manual_ingest_retry_without_mutating_recovery_state(
    from_scratch,
):
    app, store, executor, _ = application()
    source = await store.create(
        "ingest", context={"revision_key": {"historical": True}}
    )
    await store.transition(
        source.task_id, TaskStatus.FAILED, attempts=3, resume_stage="indexing"
    )
    before = await store.require(source.task_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/tasks/{source.task_id}/retry", params={"from_scratch": from_scratch}
        )
        read = await client.get(f"/tasks/{source.task_id}")
    assert response.status_code == 503 and read.status_code == 200
    assert await store.require(source.task_id) == before
    assert executor.submitted == []


async def test_pause_allows_other_kinds_and_cancellation():
    app, store, executor, _ = application()
    other = await store.create("other")
    await store.transition(other.task_id, TaskStatus.FAILED)
    queued = await store.create("ingest")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        retried = await client.post(f"/tasks/{other.task_id}/retry")
        cancelled = await client.post(f"/tasks/{queued.task_id}/cancel")
    assert retried.status_code == 200
    assert executor.submitted == [other.task_id]
    assert cancelled.status_code == 202


async def test_resume_accepts_ingest_with_the_original_source_policy():
    app, store, executor, policy = application(accepting=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ingest",
            json={"kb_id": "kb", "source": "https://document.example/file.docx"},
        )
    assert response.status_code == 202
    policy.check.assert_called_once_with("https://document.example/file.docx")
    assert executor.submitted == [response.json()["task_id"]]
    assert len(await store.list_tasks()) == 1


@pytest.mark.parametrize("accepting", [False, True])
async def test_composition_carries_pause_without_disabling_existing_runner(accepting):
    raw = config(enabled=False).model_dump(mode="python")
    raw["ingest_policy"]["accept_new_tasks"] = accepting
    context = await build_context(
        type(config()).model_validate(raw), embedding_model=Embedding()
    )
    try:
        assert context.accept_new_ingest_tasks is accepting
        assert context.runners.kinds() == ["ingest"]
        assert context.document_store is context.revision_maintenance is None
    finally:
        await context.close()


@pytest.mark.parametrize("value", ["false", 0, None])
def test_pause_configuration_rejects_implicit_boolean_conversion(value):
    with pytest.raises(ValidationError):
        IngestPolicyConfig(accept_new_tasks=value)
