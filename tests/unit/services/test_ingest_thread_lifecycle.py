from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from comet_rag.engines.chunkers import ChunkDraft
from comet_rag.engines.pipelines import AsyncHookRegistry, PipelineConfig
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.infrastructure.sources import AsyncBaseLoader
from comet_rag.ports import (
    ExtractedDocument,
    LoadedResource,
    NormalizedDocument,
    SourceContent,
)
from comet_rag.services.ingestion import INGEST_KIND, IngestRunner
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.tasks import InMemoryTaskStore, TaskContext, TaskStatus

TEXT = "A complete document."


class ThreadProbe:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.failure: Exception | None = None

    def run[T](self, operation: Callable[[], T]) -> T:
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            assert self.release.wait(2), "thread watchdog"
            if self.failure is not None:
                raise self.failure
            return operation()
        finally:
            self.finished.set()


class Embedding(AsyncBaseEmbeddingModel):
    async def _embed(self, data, **kwargs) -> list[float]:
        return [1.0]


class Loader(AsyncBaseLoader):
    def __init__(self, resource: LoadedResource) -> None:
        self.resource = resource

    async def _load(self, source, **kwargs) -> LoadedResource:
        return self.resource

    async def cleanup(self) -> None:
        return None


def runner(
    resource: LoadedResource, hooks: AsyncHookRegistry, *, normalizer=None
) -> IngestRunner:
    vectors = AsyncInMemoryVectorStore()
    knowledge_base = KnowledgeBaseService(
        repository=InMemoryKnowledgeBaseRepository(),
        vector_store=vectors,
        embedding_model="test",
        embedding_dim=1,
    )
    return IngestRunner(
        embedding_model=Embedding(),
        vector_store=vectors,
        knowledge_base=knowledge_base,
        loader=Loader(resource),
        hooks=hooks,
        normalizer=normalizer,
    )


async def context() -> TaskContext:
    store = InMemoryTaskStore()
    task = await store.create(
        INGEST_KIND, request={"kb_id": "kb", "source": "test://source"}
    )
    await store.transition(task.task_id, TaskStatus.RUNNING)
    return TaskContext(store, task.task_id)


def hooks() -> AsyncHookRegistry:
    registry = AsyncHookRegistry()

    @registry.extractor("stub")
    async def extract(
        content: LoadedResource, config: PipelineConfig
    ) -> ExtractedDocument:
        return ExtractedDocument(TEXT)

    @registry.document_chunker("stub")
    def chunk(document: NormalizedDocument, config: PipelineConfig) -> list[ChunkDraft]:
        return [ChunkDraft(document.markdown, 0, 0, len(document.markdown))]

    return registry


def resource(tmp_path: Path) -> LoadedResource:
    path = tmp_path / "download.stub"
    path.write_text(TEXT)
    return LoadedResource(
        path,
        SourceContent("test://source"),
        is_temp=True,
        metadata={"file_type": "stub"},
    )


@pytest.mark.parametrize("stage", ["normalization", "chunking"])
@pytest.mark.parametrize(
    ("cancellations", "thread_fails"), [(1, False), (2, False), (2, True)]
)
async def test_cancelled_stage_drains_thread_before_returning_or_releasing_input(
    tmp_path: Path, stage: str, cancellations: int, thread_fails: bool
) -> None:
    loaded = resource(tmp_path)
    registry = hooks()
    probe = ThreadProbe()
    if thread_fails:
        probe.failure = RuntimeError("synchronous work failed after cancellation")
    input_available: list[bool] = []

    class Normalizer:
        def normalize(self, extracted: ExtractedDocument) -> NormalizedDocument:
            def normalize() -> NormalizedDocument:
                input_available.append(loaded.path.exists())
                return NormalizedDocument(extracted.markdown)

            return probe.run(normalize)

    sut = runner(
        loaded, registry, normalizer=Normalizer() if stage == "normalization" else None
    )
    ctx = await context()
    if stage == "normalization":
        running = asyncio.create_task(sut._extract(ctx))  # noqa: SLF001 — 阶段边界故障注入
    else:

        @registry.document_chunker("stub")
        def blocked_chunk(
            document: NormalizedDocument, config: PipelineConfig
        ) -> list[ChunkDraft]:
            return probe.run(
                lambda: [ChunkDraft(document.markdown, 0, 0, len(document.markdown))]
            )

        await ctx.put(text=TEXT, file_type="stub")
        running = asyncio.create_task(sut._chunk(ctx))  # noqa: SLF001 — 阶段边界故障注入
    try:
        await asyncio.wait_for(probe.entered.wait(), 1)
        for _ in range(cancellations):
            running.cancel()
            await asyncio.sleep(0)
        _, pending = await asyncio.wait({running}, timeout=0.01)
        assert running in pending, "阶段在同步工作结束之前退出"
        assert not probe.finished.is_set()
        if stage == "normalization":
            assert loaded.path.exists(), "正规化线程仍在使用输入时提前清理"
    finally:
        probe.release.set()
        outcome = (await asyncio.gather(running, return_exceptions=True))[0]

    assert isinstance(outcome, asyncio.CancelledError)
    assert probe.finished.is_set()
    snapshot = await ctx.snapshot()
    if stage == "normalization":
        assert input_available == ([] if thread_fails else [True])
        assert "text" not in snapshot.context
        assert not loaded.path.exists()
    else:
        assert snapshot.context["text"] == TEXT
        assert "chunk_drafts" not in snapshot.context


async def test_loaded_file_cleanup_yields_event_loop_and_drains_on_cancellation(
    tmp_path: Path,
) -> None:
    loaded = resource(tmp_path)
    probe = ThreadProbe()
    loaded._release = lambda: probe.run(lambda: None)  # noqa: SLF001 — 模拟文件清理的同步阻塞
    ctx = await context()
    sut = runner(loaded, hooks())
    running = asyncio.create_task(sut._extract(ctx))  # noqa: SLF001
    try:
        await asyncio.wait_for(probe.entered.wait(), 3)
        assert not running.done(), "同步清理阻塞了事件循环直至 watchdog 失败"
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        assert not probe.finished.is_set()
    finally:
        probe.release.set()
        outcome = (await asyncio.gather(running, return_exceptions=True))[0]
    assert isinstance(outcome, asyncio.CancelledError)
    assert probe.finished.is_set()
    assert not loaded.path.exists()


@pytest.mark.parametrize("stage", ["normalization", "chunking"])
@pytest.mark.parametrize(
    "cancel_first", [False, True], ids=["shutdown", "cancel-then-shutdown"]
)
async def test_context_waits_for_ingest_thread_before_terminal_state_and_resource_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, cancel_first: bool
) -> None:
    from comet_rag.composition.bootstrap import build_context
    from comet_rag.config.schemas import (
        APPConfig,
        EmbeddingModelConfig,
        InfrastructureConfig,
        ServerConfig,
    )
    from comet_rag.infrastructure.sources import AsyncAutoLoader, AsyncLoaderRoute
    from comet_rag.services.knowledge_base import KnowledgeBaseSpec
    from tests.contracts.support import wait_for_terminal

    loaded = resource(tmp_path)
    probe = ThreadProbe()
    closed: list[str] = []

    class OwnedEmbedding(Embedding):
        async def close(self) -> None:
            closed.append("embedding")

    class OwnedLoader(Loader):
        async def cleanup(self) -> None:
            closed.append("loader")

    loader = AsyncAutoLoader(
        [AsyncLoaderRoute.schemes("test", OwnedLoader(loaded), "test")]
    )
    settings = APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="test", dim=1
            )
        ),
    )
    ctx = await build_context(
        settings, embedding_model=OwnedEmbedding(), ingest_loader=loader
    )
    await ctx.knowledge_base.create(KnowledgeBaseSpec(kb_id="kb"))

    @ctx.pipeline_hooks.extractor("stub")
    async def extract(
        content: LoadedResource, config: PipelineConfig
    ) -> ExtractedDocument:
        return ExtractedDocument(TEXT)

    ingest = ctx.runners.get(INGEST_KIND)
    assert isinstance(ingest, IngestRunner)
    if stage == "normalization":

        class Normalizer:
            def normalize(self, extracted: ExtractedDocument) -> NormalizedDocument:
                return probe.run(lambda: NormalizedDocument(extracted.markdown))

        monkeypatch.setattr(ingest, "_normalizer", Normalizer())
    else:

        @ctx.pipeline_hooks.document_chunker("stub")
        def blocked_chunk(
            document: NormalizedDocument, config: PipelineConfig
        ) -> list[ChunkDraft]:
            return probe.run(
                lambda: [ChunkDraft(document.markdown, 0, 0, len(document.markdown))]
            )

    closing: asyncio.Task[None] | None = None
    try:
        task = await ctx.task_service.submit(
            INGEST_KIND, {"kb_id": "kb", "source": "test://source"}
        )
        await asyncio.wait_for(probe.entered.wait(), 1)
        if cancel_first:
            assert await ctx.task_service.cancel(task.task_id)
        closing = asyncio.create_task(ctx.close())
        _, pending = await asyncio.wait({closing}, timeout=0.01)
        assert closing in pending, "Context 在线程完成前关闭"
        snapshot = await ctx.task_store.require(task.task_id)
        assert snapshot.status is TaskStatus.CANCELLING
        assert closed == [], "工作仍在执行时关闭了 Loader 或模型"
        assert not probe.finished.is_set()
        if stage == "normalization":
            assert loaded.path.is_file()
    finally:
        probe.release.set()
        if closing is not None:
            await asyncio.wait_for(closing, 1)
        else:
            await ctx.close()

    terminal = await wait_for_terminal(ctx.task_store, task.task_id)
    assert terminal.status is TaskStatus.CANCELLED
    assert probe.finished.is_set()
    assert closed == ["loader", "embedding"]
    assert not loaded.path.exists()
    assert ctx.runners.kinds() == []
    assert await ctx.vector_store.count("kb") == 0
