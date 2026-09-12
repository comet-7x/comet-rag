"""组合根与应用上下文：装配、后端选择、逆序关停。

关停顺序是最容易写对又最容易在重构中悄悄写坏的地方 —— 顺序错了不会立刻
报错，只会在关停瞬间偶发"连接已关闭"，而那时进程正在退出、日志往往丢失。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from comet_rag.api.routes.admin import limits as admin_limits
from comet_rag.composition.bootstrap import (
    build_context,
    build_embedding_model,
    build_mineru_extractor,
    build_vector_store,
    wire_pdf_extractor,
)
from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    IngestPolicyConfig,
    MinerUConfig,
    S3Config,
    ServerConfig,
    VectorDatabaseConfig,
)
from comet_rag.core.concurrency import Gate
from comet_rag.engines.documents.formats import ContentTypeMismatch
from comet_rag.engines.pipelines import (
    DocxConfig,
    PipelineConfig,
    PipelineHooks,
)
from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.models.reranker.base import BaseReranker
from comet_rag.infrastructure.persistence.vector_store import InMemoryVectorStore
from comet_rag.infrastructure.sources import (
    AutoLoader,
    LoaderContent,
    LocalLoader,
    SourceContent,
)
from comet_rag.infrastructure.sources.s3 import S3Loader
from comet_rag.pipeline import Pipeline
from comet_rag.ports import ExtractedDocument, MediaResource, MultimodalEmbeddingPort
from comet_rag.ports.gate import GatedResource
from comet_rag.services.ingestion import IngestRunner
from tests.fixtures.pdf import build_minimal_pdf

DIM = 3


def make_config(**backend_overrides: Any) -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=8000),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://fake/v1", model_name="fake-embed", dim=DIM
            )
        ),
        backends=BackendsConfig(**backend_overrides),
    )


def enable_mineru(config: APPConfig, **overrides: Any) -> None:
    config.infrastructure_config.mineru = MinerUConfig(
        enabled=True,
        base_url="https://mineru.test/api",
        **overrides,
    )


class FakeEmbedding(BaseEmbeddingModel):
    def __init__(self) -> None:
        self.closed = False

    def _embed(self, data, **kwargs):  # pragma: no cover
        return [0.0] * DIM

    async def _aembed(self, data, **kwargs):
        return [0.0] * DIM

    async def close_client(self) -> None:
        self.closed = True


class FakeReranker(BaseReranker):
    def __init__(self) -> None:
        self.closed = False

    def _score(self, query, documents, **kwargs):  # pragma: no cover
        return []

    async def _ascore(self, query, documents, **kwargs):  # pragma: no cover
        return []

    async def aclose(self) -> None:
        self.closed = True


class FakePdfExtractor(GatedResource):
    def __init__(
        self, *, markdown: str = "# PDF", close_order: list[str] | None = None
    ) -> None:
        self.calls: list[tuple[Path, str, str]] = []
        self.closed = False
        self.markdown = markdown
        self._close_order = close_order

    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls.append((path, filename, media_type))
        return ExtractedDocument(markdown=self.markdown)

    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls.append((path, filename, media_type))
        return ExtractedDocument(markdown=self.markdown)

    async def aclose(self) -> None:
        self.closed = True
        if self._close_order is not None:
            self._close_order.append("mineru")


@pytest.fixture
def embedding() -> FakeEmbedding:
    return FakeEmbedding()


@pytest.fixture
def context(embedding: FakeEmbedding):
    return build_context(
        make_config(), embedding_model=embedding, vector_store=InMemoryVectorStore()
    )


# ── 装配 ───────────────────────────────────────────────────────────────────


async def test_built_embedding_model_receives_source_policy() -> None:
    """多模态 embedding 也会让模型服务抓取 URL，不能漏掉 SSRF 准入策略。

    先 `isinstance` 再调用，不是为了让类型检查闭嘴：`build_embedding_model`
    的返回类型是 `EmbeddingPort`，而多模态是**可选能力**。这行断言同时把
    "当前配置装出来的模型确实支持图片"一并钉住 —— 哪天它退化成纯文本，
    这条 SSRF 用例会立刻失败而不是静静地不再检查任何东西。
    """
    model = build_embedding_model(make_config())
    try:
        assert isinstance(model, MultimodalEmbeddingPort)
        with pytest.raises(CometRAGException, match="非公网地址"):
            model.embed_media(MediaResource(url="http://127.0.0.1/metadata"))
    finally:
        await model.aclose()


async def test_built_embedding_model_rejects_local_image_by_default(
    tmp_path: Path,
) -> None:
    image = tmp_path / "private.png"
    image.write_bytes(b"png-bytes")
    model = build_embedding_model(make_config())
    try:
        assert isinstance(model, MultimodalEmbeddingPort)
        with pytest.raises(CometRAGException, match="未开放从服务器本地路径入库"):
            model.embed_media(MediaResource(path=image, mimetype="image/png"))
    finally:
        await model.aclose()


async def test_memory_backends_need_no_middleware(context) -> None:
    """全内存装配必须零外部依赖 —— 这是 plan"先内存后真实"成立的前提。"""
    assert isinstance(context.vector_store, InMemoryVectorStore)
    assert context.task_service is not None
    assert context.retrieval is not None
    assert context.retrieval._keyword_search is context.vector_store  # noqa: SLF001
    assert context.embedding_dim == DIM

    await context.aclose()


async def test_ingest_runner_is_registered(context) -> None:
    from comet_rag.tasks import registered_kinds

    assert "ingest" in registered_kinds()

    await context.aclose()


def test_injected_pipeline_config_keeps_deployment_docx_limits(
    embedding: FakeEmbedding, monkeypatch
) -> None:
    config = make_config()
    config.limits.docx_max_archive_members = 17
    config.limits.docx_max_archive_xml_elements = 19
    captured: dict[str, PipelineConfig] = {}

    def capture_runners(context, *, ingest_config):
        captured["config"] = ingest_config

    monkeypatch.setattr("comet_rag.composition.bootstrap.wire_runners", capture_runners)
    build_context(
        config,
        embedding_model=embedding,
        vector_store=InMemoryVectorStore(),
        pipeline_config=PipelineConfig(chunk_size=321),
    )

    effective = captured["config"]
    assert effective.chunk_size == 321
    assert effective.docx.max_archive_members == 17
    assert effective.docx.max_archive_xml_elements == 19


def test_explicit_docx_pipeline_fields_override_deployment_defaults(
    embedding: FakeEmbedding, monkeypatch
) -> None:
    config = make_config()
    config.limits.docx_max_archive_members = 17
    config.limits.docx_max_archive_xml_elements = 19
    captured: dict[str, PipelineConfig] = {}

    def capture_runners(context, *, ingest_config):
        captured["config"] = ingest_config

    monkeypatch.setattr("comet_rag.composition.bootstrap.wire_runners", capture_runners)
    build_context(
        config,
        embedding_model=embedding,
        vector_store=InMemoryVectorStore(),
        pipeline_config=PipelineConfig(
            docx=DocxConfig(max_archive_members=23),
        ),
    )

    effective = captured["config"]
    assert effective.docx.max_archive_members == 23
    assert effective.docx.max_archive_xml_elements == 19


async def test_s3_loader_is_assembled_only_from_infrastructure_config(
    embedding: FakeEmbedding,
) -> None:
    config = make_config()
    config.infrastructure_config.s3 = S3Config(
        endpoint_url="http://localhost:9010",
        access_key_id="minioadmin",
        secret_access_key="minioadmin",  # noqa: S106 - test credential
        max_object_bytes=1234,
    )
    config.ingest_policy = IngestPolicyConfig(
        allow_s3=True, allowed_s3_buckets=["documents"]
    )

    context = build_context(config, embedding_model=embedding)
    assert context.ingest_loader is not None
    object_loader = context.ingest_loader._resolve(  # noqa: SLF001
        SourceContent("s3://documents/report.txt")
    )
    assert isinstance(object_loader, S3Loader)
    assert object_loader._max_object_bytes == 1234  # noqa: SLF001

    await context.aclose()


def test_allowing_s3_without_connection_config_fails_at_startup(
    embedding: FakeEmbedding,
) -> None:
    config = make_config()
    config.ingest_policy = IngestPolicyConfig(allow_s3=True)

    with pytest.raises(ValueError, match="infrastructure_config.s3"):
        build_context(config, embedding_model=embedding)


async def test_mineru_is_not_built_or_registered_when_disabled(
    embedding: FakeEmbedding,
) -> None:
    context = build_context(make_config(), embedding_model=embedding)

    assert context.mineru_gate is None
    assert context.extracted_text_limits == {}
    with pytest.raises(ValueError, match="No extractor registered"):
        PipelineHooks.get_extractor("pdf")

    await context.aclose()


async def test_enabled_mineru_binds_an_independent_gate_and_pdf_hooks(
    embedding: FakeEmbedding,
    tmp_path: Path,
) -> None:
    config = make_config()
    enable_mineru(config)
    config.limits.mineru_concurrency = 2
    config.limits.mineru_queue = 5
    config.limits.mineru_wait_timeout = 7.0
    extractor = FakePdfExtractor()
    context = build_context(
        config,
        embedding_model=embedding,
        mineru_extractor=extractor,
    )

    assert isinstance(context.mineru_gate, Gate)
    assert context.mineru_gate is not context.model_gate
    assert context.mineru_gate.stats.limit == 2
    assert context.mineru_gate.name == "mineru"
    assert context.mineru_gate._max_waiting == 5  # noqa: SLF001
    assert context.mineru_gate._timeout == 7.0  # noqa: SLF001
    assert extractor._gate is context.mineru_gate  # noqa: SLF001

    path = build_minimal_pdf(tmp_path / "report.pdf")
    content = LoaderContent(
        path=path,
        source=SourceContent(path),
        metadata={"file_name": "original.pdf", "file_type": "pdf"},
    )
    assert (
        context.pipeline_hooks.get_extractor("pdf")(content, PipelineConfig())
        == "# PDF"
    )
    async_hook = context.pipeline_hooks.get_aextractor("pdf")
    assert async_hook is not None
    assert await async_hook(content, PipelineConfig()) == "# PDF"
    assert extractor.calls == [
        (path, "original.pdf", "application/pdf"),
        (path, "original.pdf", "application/pdf"),
    ]

    await context.aclose()
    assert extractor.closed
    with pytest.raises(ValueError, match="No extractor registered"):
        context.pipeline_hooks.get_extractor("pdf")


async def test_mineru_hooks_are_isolated_between_contexts_and_disabled_restart(
    embedding: FakeEmbedding,
    tmp_path: Path,
) -> None:
    enabled = make_config()
    enable_mineru(enabled)
    first_extractor = FakePdfExtractor(markdown="# first")
    second_extractor = FakePdfExtractor(markdown="# second")
    first = build_context(
        enabled, embedding_model=embedding, mineru_extractor=first_extractor
    )
    second = build_context(
        enabled, embedding_model=embedding, mineru_extractor=second_extractor
    )
    path = build_minimal_pdf(tmp_path / "isolated.pdf")
    content = LoaderContent(
        path=path,
        source=SourceContent(path),
        metadata={"file_name": path.name, "file_type": "pdf"},
    )

    assert (
        first.pipeline_hooks.get_extractor("pdf")(content, PipelineConfig())
        == "# first"
    )
    assert (
        second.pipeline_hooks.get_extractor("pdf")(content, PipelineConfig())
        == "# second"
    )
    with pytest.raises(ValueError, match="No extractor registered"):
        PipelineHooks.get_extractor("pdf")

    await first.aclose()
    disabled = build_context(make_config(), embedding_model=embedding)
    assert first_extractor.closed
    assert not second_extractor.closed
    with pytest.raises(ValueError, match="No extractor registered"):
        disabled.pipeline_hooks.get_extractor("pdf")
    assert (
        second.pipeline_hooks.get_extractor("pdf")(content, PipelineConfig())
        == "# second"
    )

    await second.aclose()
    await disabled.aclose()


async def test_pdf_pipeline_sync_and_async_use_the_document_extractor_port(
    tmp_path: Path,
) -> None:
    path = build_minimal_pdf(tmp_path / "report.pdf")
    extractor = FakePdfExtractor()
    wire_pdf_extractor(extractor)
    pipeline = Pipeline(loader=LocalLoader(), config=PipelineConfig(chunk_overlap=0))

    sync_result = pipeline.run(path)
    async_result = await pipeline.arun(path)

    assert sync_result.file_type == "pdf"
    assert async_result.file_type == "pdf"
    assert [chunk.text for chunk in sync_result.chunks] == ["# PDF"]
    assert [chunk.text for chunk in async_result.chunks] == ["# PDF"]
    assert [call[0] for call in extractor.calls] == [path, path]


async def test_pdf_content_is_rechecked_before_calling_the_extractor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forged.pdf"
    path.write_text("This is plain text, not a PDF.", encoding="utf-8")
    extractor = FakePdfExtractor()
    wire_pdf_extractor(extractor)
    content = LoaderContent(
        path=path,
        source=SourceContent(path),
        metadata={"file_name": path.name, "file_type": "pdf"},
    )

    with pytest.raises(ContentTypeMismatch, match="pdf.*txt"):
        PipelineHooks.get_extractor("pdf")(content, PipelineConfig())
    async_hook = PipelineHooks.get_aextractor("pdf")
    assert async_hook is not None
    with pytest.raises(ContentTypeMismatch, match="pdf.*txt"):
        await async_hook(content, PipelineConfig())

    assert extractor.calls == [], "伪造 PDF 不得上传到外部解析服务"


async def test_admin_limits_exposes_enabled_mineru_gate(
    embedding: FakeEmbedding,
) -> None:
    config = make_config()
    enable_mineru(config)
    context = build_context(
        config,
        embedding_model=embedding,
        mineru_extractor=FakePdfExtractor(),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=context)),
    )

    body = await admin_limits(request)  # type: ignore[arg-type]

    assert body["mineru_gate"]["limit"] == config.limits.mineru_concurrency
    await context.aclose()


async def test_mineru_builder_forwards_all_provider_settings() -> None:
    config = make_config()
    enable_mineru(
        config,
        backend="vlm-http-client",
        parse_method="ocr",
        language="en",
        formula=False,
        table=False,
        connect_timeout_seconds=3.0,
        request_timeout_seconds=7.0,
        upload_timeout_seconds=11.0,
        poll_interval_seconds=0.5,
        parse_timeout_seconds=13.0,
        max_response_bytes=1024,
        max_markdown_bytes=512,
    )

    extractor = build_mineru_extractor(config)
    assert extractor is not None
    assert extractor._backend == "vlm-http-client"  # noqa: SLF001
    assert extractor._parse_method == "ocr"  # noqa: SLF001
    assert extractor._language == "en"  # noqa: SLF001
    assert extractor._formula is False  # noqa: SLF001
    assert extractor._table is False  # noqa: SLF001
    assert extractor._connect_timeout_seconds == 3.0  # noqa: SLF001
    assert extractor._request_timeout_seconds == 7.0  # noqa: SLF001
    assert extractor._upload_timeout_seconds == 11.0  # noqa: SLF001
    assert extractor._poll_interval_seconds == 0.5  # noqa: SLF001
    assert extractor._parse_timeout_seconds == 13.0  # noqa: SLF001
    assert extractor._max_response_bytes == 1024  # noqa: SLF001
    assert extractor._max_markdown_bytes == 512  # noqa: SLF001
    await extractor.aclose()


def test_enabled_mineru_propagates_pdf_task_context_limit(
    embedding: FakeEmbedding,
) -> None:
    config = make_config()
    enable_mineru(config, max_response_bytes=2048, max_markdown_bytes=1024)

    context = build_context(
        config,
        embedding_model=embedding,
        mineru_extractor=FakePdfExtractor(),
    )

    assert context.extracted_text_limits == {"pdf": 1024}
    from comet_rag.tasks import get_runner

    runner = get_runner("ingest")
    assert isinstance(runner, IngestRunner)
    assert runner._max_extracted_text_bytes_by_type == {"pdf": 1024}  # noqa: SLF001


async def test_runner_registration_is_idempotent(embedding: FakeEmbedding) -> None:
    """应用重启（或测试反复装配）不该因重复注册而崩。"""
    first = build_context(make_config(), embedding_model=embedding)
    second = build_context(make_config(), embedding_model=embedding)

    await first.aclose()
    await second.aclose()


async def test_reranker_is_optional(context) -> None:
    """没配 reranker 时检索仍可用，只是不重排。"""
    assert context.reranker is None

    await context.aclose()


async def test_explicit_reranker_is_used(embedding: FakeEmbedding) -> None:
    reranker = FakeReranker()

    context = build_context(make_config(), embedding_model=embedding, reranker=reranker)

    assert context.reranker is reranker
    await context.aclose()


def test_unknown_backend_is_rejected(embedding: FakeEmbedding) -> None:
    """配错后端要在启动时就炸，而不是等第一个请求进来才发现。"""
    config = make_config(vector_store=Backend.POSTGRES)

    with pytest.raises(ValueError, match="不支持的 vector_store"):
        build_context(config, embedding_model=embedding)


def test_milvus_backend_requires_connection_settings(
    embedding: FakeEmbedding,
) -> None:
    config = make_config(vector_store=Backend.MILVUS)

    with pytest.raises((ValueError, ImportError, ModuleNotFoundError)):
        build_context(config, embedding_model=embedding)


def test_milvus_database_and_prefix_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comet_rag.infrastructure.persistence.vector_store import milvus

    captured: dict[str, Any] = {}

    class FakeMilvusStore(InMemoryVectorStore):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__()
            captured.update(kwargs)

    monkeypatch.setattr(milvus, "MilvusStore", FakeMilvusStore)
    config = make_config(vector_store=Backend.MILVUS)
    config.infrastructure_config.vector_database = VectorDatabaseConfig(
        endpoint="http://milvus.invalid:19530",
        database_name="zhihao_test_database",
        collection_prefix="ct_bootstrap",
    )

    store = build_vector_store(config)

    assert isinstance(store, FakeMilvusStore)
    assert captured == {
        "endpoint": "http://milvus.invalid:19530",
        "api_key": None,
        "database_name": "zhihao_test_database",
        "prefix": "ct_bootstrap",
        "replica_number": 1,
    }


# ── 关停 ───────────────────────────────────────────────────────────────────


async def test_aclose_stops_executor_before_downstream(
    embedding: FakeEmbedding,
) -> None:
    """**先停执行器，再拆它脚下的地板**。

    反过来的话，在途任务会撞上"连接已关闭"——而且只在关停瞬间偶发。
    """
    order: list[str] = []

    class RecordingStore(InMemoryVectorStore):
        async def aclose(self) -> None:
            order.append("vector_store")

    context = build_context(
        make_config(), embedding_model=embedding, vector_store=RecordingStore()
    )
    original_shutdown = context.task_executor.shutdown

    async def recording_shutdown(**kwargs):
        order.append("executor")
        await original_shutdown(**kwargs)

    context.task_executor.shutdown = recording_shutdown  # type: ignore[method-assign]

    await context.aclose()

    assert order == ["executor", "vector_store"]


async def test_aclose_closes_models(embedding: FakeEmbedding) -> None:
    reranker = FakeReranker()
    context = build_context(make_config(), embedding_model=embedding, reranker=reranker)

    await context.aclose()

    assert embedding.closed is True
    assert reranker.closed is True


async def test_aclose_stops_tasks_then_closes_mineru_before_downstream(
    embedding: FakeEmbedding,
) -> None:
    order: list[str] = []

    class RecordingStore(InMemoryVectorStore):
        async def aclose(self) -> None:
            order.append("vector_store")

    config = make_config()
    enable_mineru(config)
    extractor = FakePdfExtractor(close_order=order)
    context = build_context(
        config,
        embedding_model=embedding,
        vector_store=RecordingStore(),
        mineru_extractor=extractor,
    )
    original_shutdown = context.task_executor.shutdown

    async def recording_shutdown(**kwargs):
        order.append("executor")
        await original_shutdown(**kwargs)

    context.task_executor.shutdown = recording_shutdown  # type: ignore[method-assign]

    await context.aclose()

    assert order[:3] == ["executor", "mineru", "vector_store"]


async def test_one_failing_resource_does_not_block_the_rest(
    embedding: FakeEmbedding,
) -> None:
    """一个坏掉的连接不该让进程留下一堆泄漏资源。"""

    class ExplodingStore(InMemoryVectorStore):
        async def aclose(self) -> None:
            raise RuntimeError("关闭时炸了")

    context = build_context(
        make_config(), embedding_model=embedding, vector_store=ExplodingStore()
    )

    await context.aclose()  # 不抛即通过

    assert embedding.closed is True, "上游炸了，下游仍必须被释放"


async def test_aclose_is_idempotent(context) -> None:
    """关停路径可能被走两次（异常 + finally），不能第二次就炸。"""
    await context.aclose()
    await context.aclose()


# ── 闸门必须由组合根挂上（spec S4-2）──────────────────────────────────────


def test_build_context_binds_one_gate_to_both_models() -> None:
    """闸门只在组合根这一处挂。**漏挂不会报错，只是限流悄悄失效** ——
    所以必须有一条用例盯着这个动作本身。

    这条是补写的：最初只测了 `Gate` 本身与模型层，把 bootstrap 里那行
    `bind_gate` 删掉，全套测试照样全绿 —— 于是"不允许裸调"这条验收标准
    实际上没有任何东西守着。
    """
    embedding, reranker = FakeEmbedding(), FakeReranker()
    context = build_context(
        make_config(),
        embedding_model=embedding,
        reranker=reranker,
        vector_store=InMemoryVectorStore(),
    )

    gate = context.model_gate
    assert gate is not None, "组合根没建闸门"
    assert embedding._gate is gate, "embedding 模型没挂上闸门 —— 它会裸调模型服务"  # noqa: SLF001
    assert reranker._gate is gate, "reranker 没挂上闸门"  # noqa: SLF001
    assert gate.stats.limit == make_config().limits.model_concurrency


def test_injected_test_doubles_get_the_gate_too() -> None:
    """注入进来的替身同样要挂闸门。

    否则测试跑的是"没有闸门"那条路、生产跑的是另一条 —— 两边行为不一致，
    再多的测试也证明不了生产的限流是对的。
    """
    embedding = FakeEmbedding()
    assert embedding._gate is None  # noqa: SLF001
    build_context(
        make_config(), embedding_model=embedding, vector_store=InMemoryVectorStore()
    )
    assert embedding._gate is not None  # noqa: SLF001


def test_startup_fails_when_the_gate_does_not_stick() -> None:
    """**闸门没挂上就拒绝启动**（PR 评审 #9/#12）。

    静态守卫拦得住仓库内直接 new 模型的写法，但拦不住**注入进来的实现**：
    某个子类把 `bind_gate` 覆写成空操作，AST 检查看不见。

    闸门是"静默失效"型保护 —— 没挂上不报错、不打日志，只是限流不生效。
    宁可起不来，也别带着一个失效的闸门上线。
    """

    class SilentlyUngated(FakeEmbedding):
        def bind_gate(self, gate) -> None:  # 假装挂了，其实没有
            return None

    with pytest.raises(RuntimeError, match="没有正确挂上并发闸门"):
        build_context(
            make_config(),
            embedding_model=SilentlyUngated(),
            vector_store=InMemoryVectorStore(),
        )


def test_reranker_is_checked_too() -> None:
    class SilentlyUngatedReranker(FakeReranker):
        def bind_gate(self, gate) -> None:
            return None

    with pytest.raises(RuntimeError, match="没有正确挂上并发闸门"):
        build_context(
            make_config(),
            embedding_model=FakeEmbedding(),
            reranker=SilentlyUngatedReranker(),
            vector_store=InMemoryVectorStore(),
        )


# ── 并发配置必须真的到达管道 ───────────────────────────────────────────────


def test_pipeline_concurrency_reaches_the_runner_from_config() -> None:
    """**YAML 里调并发，必须真的作用到管道上。**

    此前 `bootstrap` 造 `PipelineConfig(docx=configured_docx)` 时只接了 docx
    限额，`max_concurrency` 与 `embed_batch_size` 一个都没接 —— 那两个数字硬
    编码在 `engines/pipelines/types.py` 里，**配置根本够不着**。

    配置项在那里、写了值、然后不起作用，比没有这个配置项更糟：调小它的人会以为
    自己限住了什么。
    """
    config = make_config()
    config.limits.pipeline_concurrency = 3
    config.limits.embed_batch_size = 7

    captured: list[PipelineConfig | None] = []

    def capture(context, *, ingest_config=None):
        captured.append(ingest_config)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("comet_rag.composition.bootstrap.wire_runners", capture)
        build_context(
            config, embedding_model=FakeEmbedding(), vector_store=InMemoryVectorStore()
        )

    assert captured
    wired = captured[0]
    assert wired is not None
    assert wired.max_concurrency == 3
    assert wired.embed_batch_size == 7


def test_explicit_pipeline_config_wins_over_deployment_limits() -> None:
    """调用方显式写过的字段不被部署配置盖掉 —— 与 docx 那套规则一致。"""
    config = make_config()
    config.limits.pipeline_concurrency = 3

    captured: list[PipelineConfig | None] = []

    def capture(context, *, ingest_config=None):
        captured.append(ingest_config)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("comet_rag.composition.bootstrap.wire_runners", capture)
        build_context(
            config,
            embedding_model=FakeEmbedding(),
            vector_store=InMemoryVectorStore(),
            pipeline_config=PipelineConfig(max_concurrency=9),
        )

    wired = captured[0]
    assert wired is not None
    assert wired.max_concurrency == 9, "显式写的 9 被部署配置盖掉了"
    # 没写过的那个仍然跟随部署配置
    assert wired.embed_batch_size == config.limits.embed_batch_size


def test_loader_gate_is_bound_to_the_leaves_not_the_router() -> None:
    """**加载侧也必须有进程级闸门，且挂在真正发请求的那一层。**

    `ingestion.py` 每个任务调一次 `aload`，`max_jobs=32` 就是 32 路对外抓取 ——
    加载侧此前完全没有上限。

    闸门挂在叶子而不是 `AutoLoader`：后者只是路由器，两层都持有会让一次加载
    取两次许可（上限为 1 时实测死锁）。
    """
    context = build_context(
        make_config(),
        embedding_model=FakeEmbedding(),
        vector_store=InMemoryVectorStore(),
    )
    loader = context.ingest_loader
    assert isinstance(loader, AutoLoader)

    assert loader._gate is None, "路由器不该持有闸门"  # noqa: SLF001
    leaf_loaders = [route.loader for route in loader.routes]
    assert all(isinstance(leaf, GatedResource) for leaf in leaf_loaders)
    leaf_gates = {
        leaf._gate  # noqa: SLF001
        for leaf in leaf_loaders
        if isinstance(leaf, GatedResource)
    }
    assert len(leaf_gates) == 1 and None not in leaf_gates, (
        f"叶子 loader 的闸门不一致或没挂上：{leaf_gates}"
    )


def test_loader_and_model_gates_are_separate_budgets() -> None:
    """**加载闸门与模型闸门是两份预算。**

    模型闸门护 GPU 排队位，加载闸门护本机文件描述符与对外连接数，合理值差一个
    数量级。共用会让"调大加载并发"意外挤掉模型的名额。

    这与"embedding 与 rerank 必须共用"不矛盾 —— 那两者抢的确实是同一块 GPU。
    **判据是资源，不是模块。**
    """
    config = make_config()
    config.limits.loader_concurrency = 3
    config.limits.model_concurrency = 9

    context = build_context(
        config, embedding_model=FakeEmbedding(), vector_store=InMemoryVectorStore()
    )
    loader = context.ingest_loader
    assert isinstance(loader, AutoLoader)

    leaf_loaders = [route.loader for route in loader.routes]
    assert all(isinstance(leaf, GatedResource) for leaf in leaf_loaders)
    loader_gate = next(
        iter(
            {
                leaf._gate  # noqa: SLF001
                for leaf in leaf_loaders
                if isinstance(leaf, GatedResource)
            }
        )
    )
    assert loader_gate is not context.model_gate, "两个闸门不该是同一个对象"
    assert isinstance(loader_gate, Gate)
    assert loader_gate.stats.limit == 3
    assert context.model_gate.stats.limit == 9
