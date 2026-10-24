from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from comet_rag.composition import bootstrap
from comet_rag.config.schemas import APPConfig, Backend, RedisConfig
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import VectorRecord
from comet_rag.services.knowledge_base import KnowledgeBaseSpec
from comet_rag.services.retrieval import SearchQuery
from comet_rag.services.revision_maintenance import RECONCILIATION_KIND
from comet_rag.tasks import InMemoryTaskStore, InProcessExecutor
from comet_rag.workers.base import build_settings
from comet_rag.workers.embedder import PROFILE as IO_PROFILE
from comet_rag.workers.preprocessor import PROFILE as CPU_PROFILE
from tests.unit.test_revision_bootstrap import Embedding, config


def configuration():
    data = config(enabled=False, milvus=True).model_dump(mode="python")
    data["backends"]["task_executor"] = Backend.ARQ
    data["infrastructure_config"]["redis"] = RedisConfig(
        host="localhost", port=6379
    ).model_dump()
    data["revision_reconciliation"] = {"enabled": True, "task_kind": "revision-test"}
    return APPConfig.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_kind", None),
        ("task_kind", " "),
        ("task_kind", RECONCILIATION_KIND),
        ("page_size", True),
        ("page_size", 33),
        ("timeout_seconds", 31),
        ("timeout_seconds", float("inf")),
    ],
)
def test_invalid_maintenance_config_is_rejected(field, value):
    data = configuration().model_dump(mode="python")
    data["revision_reconciliation"][field] = value
    with pytest.raises(ValidationError):
        APPConfig.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_executor", Backend.INPROCESS),
        ("task_store", Backend.MEMORY),
        ("vector_store", Backend.MEMORY),
    ],
)
def test_automatic_maintenance_requires_durable_cross_process_backends(field, value):
    data = configuration().model_dump(mode="python")
    data["backends"][field] = value
    with pytest.raises(ValidationError, match="requires|ARQ/PostgreSQL/Milvus"):
        APPConfig.model_validate(data)


def test_only_io_worker_registers_enabled_cron_with_lease_recovery():
    enabled = configuration()
    assert [
        job.name for job in build_settings(CPU_PROFILE, config=enabled).cron_jobs
    ] == ["sweep_stale_tasks"]
    jobs = build_settings(IO_PROFILE, config=enabled).cron_jobs
    assert [job.name for job in jobs] == ["sweep_stale_tasks", "reconcile_revisions"]
    assert jobs[1].unique and jobs[1].run_at_startup
    assert jobs[1].timeout_s == 45
    with pytest.raises(ValueError, match="租约回收"):
        build_settings(replace(IO_PROFILE, sweep=False), config=enabled)


async def test_opaque_store_injection_is_rejected_before_creating_resources(
    monkeypatch,
):
    create = Mock(side_effect=AssertionError("must fail before resource construction"))
    monkeypatch.setattr(bootstrap, "build_database", create)
    with pytest.raises(ValueError, match="不透明"):
        await bootstrap.build_context(configuration(), task_store=InMemoryTaskStore())
    create.assert_not_called()


def fake_storage_builders(monkeypatch):
    # 验装配和关闭顺序，不创建连接池；DocumentStore 仍使用真实类。
    database = Mock(spec=Database)
    database.close = AsyncMock()
    tasks, vectors = InMemoryTaskStore(), AsyncInMemoryVectorStore()
    monkeypatch.setattr(bootstrap, "build_database", lambda _: database)
    monkeypatch.setattr(bootstrap, "build_task_store", lambda *_: tasks)
    monkeypatch.setattr(bootstrap, "build_vector_store", Mock(return_value=vectors))
    monkeypatch.setattr(
        bootstrap,
        "build_task_executor",
        lambda config, store, **kwargs: InProcessExecutor(
            store, runners=kwargs["runners"]
        ),
    )
    return database, tasks, vectors


async def test_enabling_maintenance_does_not_enable_revision_reads_or_writes(
    monkeypatch,
):
    from comet_rag.api.routes.admin import limits

    _, tasks, _ = fake_storage_builders(monkeypatch)
    context = await bootstrap.build_context(
        configuration(),
        embedding_model=Embedding(),
        kb_repository=InMemoryKnowledgeBaseRepository(),
    )
    try:
        assert (
            context.document_store is not None
            and context.revision_maintenance is not None
        )
        lookup = AsyncMock(return_value={"source": "another-active-version"})
        builder = bootstrap.build_vector_store
        assert isinstance(builder, Mock)
        assert builder.call_args.kwargs["revision_aware"] is False
        monkeypatch.setattr(context.document_store, "get_active_revisions", lookup)
        await context.knowledge_base.create(KnowledgeBaseSpec(kb_id="kb"))
        await context.vector_store.upsert(
            "kb",
            [
                VectorRecord(
                    "legacy", "legacy", [1.0, 0.0, 0.0], {"source_id": "source"}
                )
            ],
        )
        response = await context.retrieval.search(
            SearchQuery(kb_id="kb", query="legacy")
        )
        assert [chunk.id for chunk in response.chunks] == ["legacy"]
        lookup.assert_not_awaited()
        assert context.runners.kinds() == ["ingest"]
        assert len(await tasks.list_tasks(kind=RECONCILIATION_KIND)) == 1
        request = Mock(app=SimpleNamespace(state=SimpleNamespace(ctx=context)))
        status = await limits(request)
        assert status["revision_reconciliation"]["status"] == "pending"
        assert status["revision_reconciliation"]["overdue"] is False
    finally:
        await context.close()


async def test_late_startup_failure_closes_resources_and_preserves_cursor(monkeypatch):
    database, tasks, vectors = fake_storage_builders(monkeypatch)
    close = AsyncMock(wraps=database.close)
    monkeypatch.setattr(database, "close", close)
    vector_close = AsyncMock(wraps=vectors.close)
    monkeypatch.setattr(vectors, "close", vector_close)
    monkeypatch.setattr(
        bootstrap, "wire_runners", Mock(side_effect=RuntimeError("registration failed"))
    )
    with pytest.raises(RuntimeError, match="registration failed"):
        await bootstrap.build_context(
            configuration(),
            embedding_model=Embedding(),
            kb_repository=InMemoryKnowledgeBaseRepository(),
        )
    close.assert_awaited_once()
    vector_close.assert_awaited_once()
    [cursor] = await tasks.list_tasks(kind=RECONCILIATION_KIND)
    assert cursor.context["revision_scan"]["before"] is None


async def test_retained_maintenance_cursor_does_not_consume_business_backlog(
    monkeypatch,
):
    from comet_rag.tasks import Done
    from comet_rag.tasks.service import Backlogged

    fake_storage_builders(monkeypatch)
    configured = configuration()
    configured.limits.max_backlog = 1
    context = await bootstrap.build_context(
        configured,
        embedding_model=Embedding(),
        kb_repository=InMemoryKnowledgeBaseRepository(),
    )
    try:

        async def probe(ctx):
            return Done()

        context.runners.register("admission-probe")(probe)
        monkeypatch.setattr(context.task_executor, "submit", AsyncMock())
        assert (await context.task_service.backlog())["pending"] == 0
        await context.task_service.submit("admission-probe")
        assert (await context.task_service.backlog())["pending"] == 1
        with pytest.raises(Backlogged):
            await context.task_service.submit("admission-probe")
    finally:
        await context.close()


async def test_business_backlog_is_filtered_before_limit_and_deduplicates_kinds():
    from comet_rag.tasks import TaskService
    from comet_rag.tasks.service import Backlogged

    tasks = InMemoryTaskStore()
    await tasks.create("a-business")
    await tasks.create("z-business")
    for _ in range(4):
        await tasks.create(RECONCILIATION_KIND)
    executor = Mock(submit=AsyncMock())
    service = TaskService(
        tasks,
        executor,
        max_backlog=3,
        pending_kinds=lambda: ["z-business", "a-business", "z-business"],
    )
    assert await service.backlog() == {
        "pending": 2,
        "at_least": False,
        "max_backlog": 3,
    }
    await service.submit("a-business")
    assert (await service.backlog())["pending"] == 3
    with pytest.raises(Backlogged):
        await service.submit("a-business")
    executor.submit.assert_awaited_once()
    # 未指定范围的库调用仍统计所有 PENDING，避免暗改通用门面契约。
    assert (await TaskService(tasks, executor, max_backlog=5).backlog())["pending"] == 5
