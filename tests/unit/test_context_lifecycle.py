from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from comet_rag.composition import bootstrap
from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    ServerConfig,
    SqlDatabaseConfig,
)
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.infrastructure.sources import AsyncAutoLoader
from comet_rag.ports.gate import GatedResource
from comet_rag.tasks import InMemoryTaskStore, InProcessExecutor
from comet_rag.tasks.runner import UnknownKind


class Embedding(AsyncBaseEmbeddingModel):
    async def _embed(self, data, **kwargs):
        return self._embed(data, **kwargs)


@dataclass
class Assembly:
    config: APPConfig
    resources: dict[str, Any]  # 故障夹具保存不同 Port 的替身，按装配参数名取用。
    created: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    fail_at: str | None = None
    close_failure: str | None = None
    error: BaseException = field(default_factory=lambda: RuntimeError("startup failed"))

    def builder(self, name: str) -> Callable[..., object]:
        def build(*args, **kwargs) -> object:
            if name == self.fail_at:
                raise self.error
            self.created.append(name)
            return self.resources[name]

        return build

    def closer(self, name: str) -> Callable[[], object]:
        async def close() -> None:
            # 真正挂起一次，确保测试要求的是已等待清理而非仅创建了协程。
            await asyncio.sleep(0)
            self.closed.append(name)
            if name == self.close_failure:
                raise RuntimeError("close failed")

        return close


@pytest.fixture
def assembly(monkeypatch: pytest.MonkeyPatch):
    config = APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=8000),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://fake/v1",
                model_name="fake",
                dim=3,
                api_key="test-secret",
            ),
            database=SqlDatabaseConfig(
                host="fake",
                port=5432,
                username="test",
                password="test-secret",  # noqa: S106 — 无网络测试凭据
                database="test",
            ),
        ),
        backends=BackendsConfig(task_store=Backend.POSTGRES),
    )
    config.retrieval.revision_aware = True
    store = InMemoryTaskStore()
    resources = {
        "database": SimpleNamespace(),
        "document_store": InMemoryDocumentStore(),
        "ingest_loader": AsyncAutoLoader.default(),
        "embedding_model": Embedding(),
        "reranker": GatedResource(),
        "mineru_extractor": GatedResource(),
        "vector_store": AsyncInMemoryVectorStore(),
        "task_store": store,
        "task_executor": InProcessExecutor(store),
        "kb_repository": InMemoryKnowledgeBaseRepository(),
    }
    result = Assembly(config, resources)
    for name, resource in resources.items():
        monkeypatch.setattr(bootstrap, f"build_{name}", result.builder(name))
        if name not in {"task_store", "kb_repository"}:
            method = {
                "task_executor": "shutdown",
                "vector_store": "close",
            }.get(name, "close")
            monkeypatch.setattr(resource, method, result.closer(name), raising=False)
    yield result


@pytest.mark.parametrize(
    ("stage", "closed"),
    [
        ("database", []),
        ("document_store", ["database"]),
        ("ingest_loader", ["document_store", "database"]),
        ("embedding_model", ["ingest_loader", "document_store", "database"]),
        (
            "reranker",
            ["embedding_model", "ingest_loader", "document_store", "database"],
        ),
        (
            "mineru_extractor",
            [
                "reranker",
                "embedding_model",
                "ingest_loader",
                "document_store",
                "database",
            ],
        ),
        (
            "vector_store",
            [
                "mineru_extractor",
                "reranker",
                "embedding_model",
                "ingest_loader",
                "document_store",
                "database",
            ],
        ),
        (
            "task_store",
            [
                "vector_store",
                "mineru_extractor",
                "reranker",
                "embedding_model",
                "ingest_loader",
                "document_store",
                "database",
            ],
        ),
        (
            "task_executor",
            [
                "vector_store",
                "mineru_extractor",
                "reranker",
                "embedding_model",
                "ingest_loader",
                "document_store",
                "database",
            ],
        ),
        (
            "kb_repository",
            [
                "task_executor",
                "vector_store",
                "mineru_extractor",
                "reranker",
                "embedding_model",
                "ingest_loader",
                "document_store",
                "database",
            ],
        ),
    ],
)
async def test_partial_construction_rolls_back_in_reverse_order(
    assembly: Assembly, stage: str, closed: list[str]
) -> None:
    assembly.fail_at = stage
    with pytest.raises(RuntimeError) as caught:
        await bootstrap.build_context(assembly.config)
    assert caught.value is assembly.error
    assert assembly.closed == closed


@pytest.mark.parametrize(
    "field", ["chunk_size", "chunk_overlap", "max_concurrency", "embed_batch_size"]
)
async def test_invalid_pipeline_is_rejected_before_creating_resources(
    assembly: Assembly, field: str
) -> None:
    config = PipelineConfig()
    setattr(config, field, -1)
    with pytest.raises(ValidationError, match=field):
        await bootstrap.build_context(assembly.config, pipeline_config=config)
    assert assembly.created == []
    assert assembly.closed == []


@pytest.mark.parametrize(
    "field", ["model_queue", "model_wait_timeout", "loader_concurrency"]
)
async def test_mutated_app_config_is_revalidated_before_creating_resources(
    assembly: Assembly, field: str
) -> None:
    setattr(assembly.config.limits, field, -1)
    with pytest.raises(ValidationError, match=field):
        await bootstrap.build_context(assembly.config)
    assert assembly.created == []


@pytest.mark.parametrize(
    "setting", ["database", "vector_database", "redis", "s3", "backend_role"]
)
async def test_missing_selected_backend_settings_fail_before_creation(
    assembly: Assembly, setting: str
) -> None:
    config = assembly.config
    config.retrieval.revision_aware = False
    if setting == "database":
        config.infrastructure_config.database = None
    elif setting == "vector_database":
        config.backends.vector_store = Backend.MILVUS
    elif setting == "redis":
        config.backends.vector_store = Backend.MILVUS
        config.backends.task_executor = Backend.ARQ
        # 显式替换向量库，避免缺失 Milvus 设置抢先触发。
    elif setting == "s3":
        config.ingest_policy.allow_s3 = True
    else:
        config.backends.vector_store = Backend.ARQ
    expected = "vector_store" if setting == "backend_role" else setting
    with pytest.raises(ValueError, match=expected):
        await bootstrap.build_context(
            config,
            vector_store=AsyncInMemoryVectorStore() if setting == "redis" else None,
        )
    assert assembly.created == []


async def test_revalidation_preserves_secrets_and_input(
    assembly: Assembly, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = assembly.config.model_dump(mode="python")

    def build_database(config: APPConfig) -> object:
        assert config is not assembly.config
        assert (
            config.infrastructure_config.embedding_model.api_key_value == "test-secret"
        )
        assert config.infrastructure_config.database is not None
        assert ":test-secret@" in config.infrastructure_config.database.dsn
        return assembly.resources["database"]

    monkeypatch.setattr(bootstrap, "build_database", build_database)
    context = await bootstrap.build_context(assembly.config)
    await context.close()
    assert assembly.config.model_dump(mode="python") == original


async def test_gate_binding_failure_rolls_back(
    assembly: Assembly, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_binding(gate: object) -> None:
        raise assembly.error

    monkeypatch.setattr(
        assembly.resources["embedding_model"], "bind_gate", fail_binding
    )
    with pytest.raises(RuntimeError) as caught:
        await bootstrap.build_context(assembly.config)
    assert caught.value is assembly.error
    assert assembly.closed == [
        "reranker",
        "embedding_model",
        "ingest_loader",
        "document_store",
        "database",
    ]


async def test_runner_failure_revokes_registration_and_preserves_original_error(
    assembly: Assembly, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = bootstrap.wire_runners

    def fail_after_registration(context, *, ingest_config):
        wire(context, ingest_config=ingest_config)
        raise assembly.error

    monkeypatch.setattr(bootstrap, "wire_runners", fail_after_registration)
    assembly.close_failure = "vector_store"
    with pytest.raises(RuntimeError) as caught:
        await bootstrap.build_context(assembly.config)
    assert caught.value is assembly.error
    assert assembly.closed == [
        "task_executor",
        "vector_store",
        "mineru_extractor",
        "reranker",
        "embedding_model",
        "ingest_loader",
        "document_store",
        "database",
    ]
    with pytest.raises(UnknownKind):
        assembly.resources["task_executor"].runners.get("ingest")


async def test_cancelled_startup_waits_for_rollback(assembly: Assembly) -> None:
    assembly.fail_at = "task_store"
    assembly.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        await bootstrap.build_context(assembly.config)
    assert caught.value is assembly.error
    assert assembly.closed == [
        "vector_store",
        "mineru_extractor",
        "reranker",
        "embedding_model",
        "ingest_loader",
        "document_store",
        "database",
    ]


async def test_failed_startup_does_not_close_injected_resources(
    assembly: Assembly,
) -> None:
    assembly.fail_at = "kb_repository"
    with pytest.raises(RuntimeError):
        await bootstrap.build_context(
            assembly.config,
            embedding_model=assembly.resources["embedding_model"],
            reranker=assembly.resources["reranker"],
            ingest_loader=assembly.resources["ingest_loader"],
            mineru_extractor=assembly.resources["mineru_extractor"],
            vector_store=assembly.resources["vector_store"],
            document_store=assembly.resources["document_store"],
            task_executor=assembly.resources["task_executor"],
        )
    assert assembly.closed == ["database"]


async def test_success_transfers_ownership_and_closes_once_in_dependency_order(
    assembly: Assembly,
) -> None:
    context = await bootstrap.build_context(assembly.config)
    assert assembly.closed == []
    await asyncio.gather(context.close(), context.close())
    await context.close()
    assert assembly.closed == [
        "task_executor",
        "mineru_extractor",
        "ingest_loader",
        "vector_store",
        "document_store",
        "reranker",
        "embedding_model",
        "database",
    ]
    with pytest.raises(UnknownKind):
        assembly.resources["task_executor"].runners.get("ingest")


async def test_synchronous_shutdown_failure_does_not_skip_other_resources(
    assembly: Assembly, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = await bootstrap.build_context(assembly.config)

    def fail_shutdown():
        assembly.closed.append("task_executor")
        raise RuntimeError("synchronous close failure")

    monkeypatch.setattr(context.task_executor, "shutdown", fail_shutdown)
    await context.close()
    assert assembly.closed == [
        "task_executor",
        "mineru_extractor",
        "ingest_loader",
        "vector_store",
        "document_store",
        "reranker",
        "embedding_model",
        "database",
    ]


async def test_reusing_executor_rejects_duplicate_runner_without_unbinding_owner(
    assembly: Assembly,
) -> None:
    first = await bootstrap.build_context(assembly.config)
    original = first.runners.get("ingest")
    try:
        with pytest.raises(ValueError, match="重复注册"):
            await bootstrap.build_context(
                assembly.config,
                **{
                    name: value
                    for name, value in assembly.resources.items()
                    if name != "database"
                },
            )
        assert first.runners.get("ingest") is original
    finally:
        await first.close()


async def test_cancelled_close_finishes_drain_before_closing_dependencies(
    assembly: Assembly, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = await bootstrap.build_context(assembly.config)
    draining = asyncio.Event()
    release = asyncio.Event()

    async def shutdown() -> None:
        draining.set()
        await release.wait()
        assembly.closed.append("task_executor")

    monkeypatch.setattr(context.task_executor, "shutdown", shutdown)
    closing = asyncio.create_task(context.close())
    await draining.wait()
    try:
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert assembly.closed == []
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
    await context.close()
    assert assembly.closed == [
        "task_executor",
        "mineru_extractor",
        "ingest_loader",
        "vector_store",
        "document_store",
        "reranker",
        "embedding_model",
        "database",
    ]


async def test_cancelled_rollback_waits_for_resource_close(
    assembly: Assembly, monkeypatch: pytest.MonkeyPatch
) -> None:
    assembly.fail_at = "reranker"
    closing_model = asyncio.Event()
    release = asyncio.Event()

    async def close_model() -> None:
        closing_model.set()
        await release.wait()
        assembly.closed.append("embedding_model")

    monkeypatch.setattr(assembly.resources["embedding_model"], "close", close_model)
    startup = asyncio.create_task(bootstrap.build_context(assembly.config))
    await closing_model.wait()
    try:
        startup.cancel()
        await asyncio.sleep(0)
        assert not startup.done()
        assert assembly.closed == []
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
    assert assembly.closed == [
        "embedding_model",
        "ingest_loader",
        "document_store",
        "database",
    ]
