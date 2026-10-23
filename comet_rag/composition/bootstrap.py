"""根据配置装配应用依赖；具体供应商只在这里选择。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from comet_rag.composition.context import Context, wire_runners
from comet_rag.composition.pipeline_config import resolve_pipeline_config
from comet_rag.composition.resources import (
    clear_registrations,
    close_resource,
    run_cleanup,
    wait_for_cleanup,
)
from comet_rag.config.schemas import APPConfig, Backend
from comet_rag.core.concurrency import Gate, build_gate
from comet_rag.core.degradation import DegradationController, DegradationSettings
from comet_rag.core.logging import logger
from comet_rag.engines.documents.docx.vision import validate_vision_model
from comet_rag.engines.documents.formats import resolve_detected_extension
from comet_rag.engines.offload import run_sync
from comet_rag.engines.pipelines import (
    AsyncHookRegistry,
    AsyncPipelineHooks,
    PipelineConfig,
)
from comet_rag.engines.utils import detect_content_type_from_path
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.infrastructure.sources import (
    AsyncAutoLoader,
    AsyncLoaderRoute,
    LoaderContent,
)
from comet_rag.ports import (
    AsyncDocumentExtractorPort,
    AsyncEmbeddingPort,
    AsyncKeywordSearchPort,
    AsyncRerankerPort,
    AsyncVectorStore,
    ExtractedDocument,
)
from comet_rag.ports.document_store import DocumentStore
from comet_rag.ports.knowledge_base import (
    KnowledgeBaseRepository,
)
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.retrieval import RetrievalService
from comet_rag.services.source_policy import SourcePolicy, build_source_policy
from comet_rag.tasks import (
    LANE_CPU,
    InMemoryTaskStore,
    InProcessExecutor,
    RunnerRegistry,
    StoreDrivenExecutor,
    TaskExecutor,
    TaskService,
    TaskStore,
)

if TYPE_CHECKING:
    from comet_rag.infrastructure.extractors import AsyncMinerUDocumentExtractor
    from comet_rag.infrastructure.persistence.sql import Database


def build_vector_store(
    config: APPConfig, *, revision_aware: bool = False
) -> AsyncVectorStore:
    backend = config.backends.vector_store
    if backend is Backend.MEMORY:
        return AsyncInMemoryVectorStore()
    if backend is Backend.MILVUS:
        # pymilvus 在 `milvus` extra 里，函数内 import 才不会拖累未安装的用户
        from comet_rag.infrastructure.persistence.vector_store.milvus import (  # noqa: PLC0415
            AsyncMilvusStore,
        )

        settings = config.infrastructure_config.vector_database
        if settings is None:
            raise ValueError(
                "backends.vector_store=milvus 但未配置 "
                "infrastructure_config.vector_database"
            )
        return AsyncMilvusStore(
            endpoint=settings.endpoint,
            api_key=settings.api_key_value,
            database_name=settings.database_name,
            prefix=settings.collection_prefix,
            replica_number=settings.replica_number,
            consistency_level="Strong" if revision_aware else "Session",
        )
    raise ValueError(f"不支持的 vector_store 后端：{backend}")


def build_database(config: APPConfig):
    """关系库引擎。只在真的需要时创建 —— 全 memory 的部署不该被迫连库。"""
    from comet_rag.infrastructure.persistence.sql import Database  # noqa: PLC0415

    settings = config.infrastructure_config.database
    if settings is None:
        raise ValueError("选择了 postgres 后端但未配置 infrastructure_config.database")
    return Database(
        settings.dsn,
        echo=settings.echo,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
    )


def build_kb_repository(config: APPConfig, database=None) -> KnowledgeBaseRepository:
    """知识库元数据仓储。

    跟着 `task_store` 走而非单独配一个开关：两者都是关系型元数据，
    分开配只会让"任务在 Postgres、知识库在内存"这种没人想要的组合成为可能。
    """
    if config.backends.task_store is Backend.MEMORY:
        return InMemoryKnowledgeBaseRepository()
    from comet_rag.infrastructure.persistence.knowledge_base.postgres import (  # noqa: PLC0415
        PostgresKnowledgeBaseRepository,
    )

    if database is None:
        raise ValueError("postgres 后端需要传入 database")
    return PostgresKnowledgeBaseRepository(database)


def build_task_store(config: APPConfig, database=None) -> TaskStore:
    backend = config.backends.task_store
    if backend is Backend.MEMORY:
        return InMemoryTaskStore()
    if backend is Backend.POSTGRES:
        from comet_rag.infrastructure.persistence.task_store.postgres import (  # noqa: PLC0415
            PostgresTaskStore,
        )

        if database is None:
            raise ValueError("postgres 后端需要传入 database")
        return PostgresTaskStore(database)
    raise ValueError(f"不支持的 task_store 后端：{backend}")


def build_document_store(
    config: APPConfig,
    database: Database | None = None,
    *,
    vector_store: AsyncVectorStore | None = None,
) -> DocumentStore:
    """跟向量资源配对；显式注入的内存向量替身不应意外创建持久化资源。"""
    memory = (
        isinstance(vector_store, AsyncInMemoryVectorStore)
        if vector_store is not None
        else config.backends.vector_store is Backend.MEMORY
    )
    if memory:
        return InMemoryDocumentStore()
    from comet_rag.infrastructure.persistence.document_store.postgres import (  # noqa: PLC0415
        PostgresDocumentStore,
    )

    if database is None:
        raise ValueError("revision-aware milvus requires database")
    return PostgresDocumentStore(database)


def _validate_revision_vectors(vectors: AsyncVectorStore) -> None:
    if isinstance(vectors, AsyncInMemoryVectorStore):
        return
    from comet_rag.infrastructure.persistence.vector_store.milvus import (  # noqa: PLC0415
        AsyncMilvusStore,
    )

    if (
        not isinstance(vectors, AsyncMilvusStore)
        or vectors.consistency_level != "Strong"
    ):
        raise ValueError(
            "injected revision vector store requires Strong AsyncMilvusStore"
        )


def _validate_revision_resources(
    config: APPConfig,
    documents: DocumentStore,
    vectors: AsyncVectorStore | None,
) -> None:
    if vectors is not None:
        _validate_revision_vectors(vectors)
    memory = (
        isinstance(vectors, AsyncInMemoryVectorStore)
        if vectors is not None
        else config.backends.vector_store is Backend.MEMORY
    )
    if memory:
        if not isinstance(documents, InMemoryDocumentStore):
            raise ValueError("memory vectors require InMemoryDocumentStore")
        return
    from comet_rag.infrastructure.persistence.document_store.postgres import (  # noqa: PLC0415
        PostgresDocumentStore,
    )

    if not isinstance(documents, PostgresDocumentStore):
        raise ValueError("milvus revision recall requires PostgresDocumentStore")


def build_task_executor(
    config: APPConfig,
    store: TaskStore,
    *,
    lane: str | None = None,
    runners: RunnerRegistry | None = None,
) -> TaskExecutor:
    """`lane` = 本进程服务哪条负载道。

    API 进程与单进程部署都传 None（不分道，只作生产端）；worker 进程由
    `workers/base.py` 按自己的画像传 cpu / io。
    """
    backend = config.backends.task_executor
    if backend is Backend.INPROCESS:
        # 单进程下分道毫无意义：移交只会变成一次没必要的入队往返，
        # 而且 InProcessExecutor 压根没有"另一条队列"可投。
        return InProcessExecutor(
            store, max_concurrency=config.backends.max_concurrency, runners=runners
        )
    if backend is Backend.ARQ:
        from comet_rag.infrastructure.task_execution.arq import (  # noqa: PLC0415
            LANE_QUEUES,
            ArqExecutor,
        )

        settings = config.infrastructure_config.redis
        if settings is None:
            raise ValueError(
                "backends.task_executor=arq 但未配置 infrastructure_config.redis"
            )
        # 注意这里**不传** max_concurrency：跨进程部署的闸门在 worker 侧
        # （arq 的 max_jobs），生产端限流拦不住别的生产端。见 executor_arq.py。
        return ArqExecutor(
            store,
            redis_url=settings.url,
            lane=lane,
            lanes=LANE_QUEUES,
            # 生产端首投进第一个阶段所在的道。投错也能自愈（目标 worker 会
            # 再移交一次），但投对能省掉一次空转往返。
            entry_lane=LANE_CPU,
            runners=runners,
        )
    raise ValueError(f"不支持的 task_executor 后端：{backend}")


def _executor_runners(
    executor: TaskExecutor, runners: RunnerRegistry | None
) -> RunnerRegistry:
    if isinstance(executor, StoreDrivenExecutor):
        if runners is not None and runners is not executor.runners:
            raise ValueError("runners 必须与注入执行器的注册表是同一实例")
        return executor.runners
    if runners is None:
        raise ValueError("注入自定义 TaskExecutor 时必须同时提供 runners 供消费者使用")
    return runners


def build_degradation(config: APPConfig) -> DegradationController:
    limits = config.limits
    return DegradationController(
        DegradationSettings(
            failure_rate=limits.degrade_failure_rate,
            saturation=limits.degrade_saturation,
            recover_after=limits.degrade_recover_after,
            min_samples=limits.degrade_min_samples,
        )
    )


def build_ingest_loader(config: APPConfig, policy: SourcePolicy) -> AsyncAutoLoader:
    """Assemble protocol-neutral routes with optional infrastructure loaders."""

    routes = AsyncAutoLoader.default_routes(
        max_download_bytes=policy.max_download_bytes,
        redirect_validator=policy.check_redirect,
    )
    settings = config.infrastructure_config.s3
    if settings is None:
        if policy.allow_s3:
            raise ValueError(
                "ingest_policy.allow_s3=true 但未配置 infrastructure_config.s3"
            )
        return AsyncAutoLoader(routes)

    from comet_rag.infrastructure.sources.s3 import AsyncS3Loader  # noqa: PLC0415

    object_loader = AsyncS3Loader(
        endpoint_url=settings.endpoint_url,
        access_key_id=settings.access_key_id_value,
        secret_access_key=settings.secret_access_key_value,
        session_token=settings.session_token_value,
        region_name=settings.region_name,
        addressing_style=settings.addressing_style.value,
        verify_ssl=settings.verify_ssl,
        max_object_bytes=settings.max_object_bytes,
    )
    routes.insert(
        0,
        AsyncLoaderRoute.schemes("s3", object_loader, {"s3", "minio"}),
    )
    return AsyncAutoLoader(routes)


def build_loader_gate(config: APPConfig) -> Gate:
    """**加载侧单独一个闸门，不与模型共用。**

    模型闸门护的是 GPU 服务的排队位，加载闸门护的是本机文件描述符与对外连接
    数 —— 两者的合理值差一个数量级。共用一个的话，"调大加载并发"会意外挤掉
    模型的名额，而这种互相踩踏在监控上看不出来。

    这跟 `build_model_gate` 里"embedding 与 rerank 必须共用"不矛盾：那两者抢
    的确实是同一块 GPU，加载抢的是另一种资源。**判据是资源，不是模块。**
    """
    return build_gate(
        limit=config.limits.loader_concurrency,
        max_waiting=config.limits.model_queue,
        acquire_timeout=config.limits.model_wait_timeout,
        name="loader",
    )


def build_mineru_gate(config: APPConfig) -> Gate:
    """MinerU 解析占用的是外部文档服务，不与加载或模型请求共享预算。"""
    limits = config.limits
    return build_gate(
        limit=limits.mineru_concurrency,
        max_waiting=limits.mineru_queue,
        acquire_timeout=limits.mineru_wait_timeout,
        name="mineru",
    )


def build_mineru_extractor(config: APPConfig) -> AsyncMinerUDocumentExtractor | None:
    settings = config.infrastructure_config.mineru
    if not settings.enabled:
        return None

    from comet_rag.infrastructure.extractors import (  # noqa: PLC0415
        AsyncMinerUDocumentExtractor,
    )

    return AsyncMinerUDocumentExtractor(
        settings.base_url,
        backend=settings.backend,
        parse_method=settings.parse_method.value,
        language=settings.language,
        formula=settings.formula,
        table=settings.table,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        request_timeout_seconds=settings.request_timeout_seconds,
        upload_timeout_seconds=settings.upload_timeout_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
        parse_timeout_seconds=settings.parse_timeout_seconds,
        max_response_bytes=settings.max_response_bytes,
        max_markdown_bytes=settings.max_markdown_bytes,
    )


def wire_pdf_extractor(
    extractor: AsyncDocumentExtractorPort, *, hooks: AsyncHookRegistry | None = None
) -> Callable[[], None]:
    """把 Port 适配到现有字符串 Hook；供应商字段不进入 engines。"""
    registry = hooks or AsyncPipelineHooks
    previous = registry.snapshot()

    def verify_content(content: LoaderContent) -> None:
        """Loader 路由只声明候选格式；外发前以实际字节为最终依据。"""
        detected = detect_content_type_from_path(str(content.path))
        resolve_detected_extension("pdf", detected)

    def filename(content: LoaderContent) -> str:
        configured = content.metadata.get("file_name")
        if isinstance(configured, str) and configured.strip():
            return configured
        return content.path.name

    @registry.extractor("pdf")
    async def extract_pdf(
        content: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        del config
        await run_sync(verify_content, content)
        return await extractor.extract(
            content.path,
            filename=filename(content),
            media_type="application/pdf",
        )

    return lambda: registry.restore(previous)


def build_model_gate(
    config: APPConfig, degradation: DegradationController | None = None
) -> Gate:
    """**一个进程一个闸门，embedding 与 rerank 共用它。**

    分开限流等于没限：两边各配 8，模型服务看到的是 16。它们抢的本来就是
    同一块 GPU，只有合起来算才对得上"这台机器最多同时处理几个请求"。
    """
    limits = config.limits
    return build_gate(
        limit=limits.model_concurrency,
        max_waiting=limits.model_queue,
        acquire_timeout=limits.model_wait_timeout,
        name="model",
        # 观测点挂在闸门上：所有对模型服务的调用都必然穿过它，
        # 于是降级判据的数据来源天然完整，不会有谁忘了插桩。
        observer=None if degradation is None else degradation.record,
    )


def build_embedding_model(
    config: APPConfig,
    *,
    image_url_validator: Callable[[str], None] | None = None,
    local_image_validator: Callable[[str], None] | None = None,
) -> AsyncEmbeddingPort:
    from comet_rag.infrastructure.models.embedding.qwen3_vl import (  # noqa: PLC0415
        AsyncQwen3VLEmbeddingModel,
    )

    settings = config.infrastructure_config.embedding_model
    if image_url_validator is None or local_image_validator is None:
        source_policy = build_source_policy(config.ingest_policy)
        image_url_validator = image_url_validator or source_policy.check_redirect
        local_image_validator = local_image_validator or source_policy.check
    return AsyncQwen3VLEmbeddingModel(
        base_url=settings.base_url,
        model_name=settings.model_name,
        api_key=settings.api_key_value or "EMPTY",
        image_url_validator=image_url_validator,
        local_image_validator=local_image_validator,
        max_local_image_bytes=config.limits.model_image_max_bytes,
    )


def build_reranker(
    config: APPConfig,
    *,
    image_url_validator: Callable[[str], None] | None = None,
    local_image_validator: Callable[[str], None] | None = None,
) -> AsyncRerankerPort | None:
    settings = config.infrastructure_config.reranker
    if settings is None:
        logger.info("未配置 reranker，检索将跳过重排")
        return None
    from comet_rag.infrastructure.models.reranker.qwen3_vl import (  # noqa: PLC0415
        AsyncQwen3VLReranker,
    )

    if image_url_validator is None or local_image_validator is None:
        source_policy = build_source_policy(config.ingest_policy)
        image_url_validator = image_url_validator or source_policy.check_redirect
        local_image_validator = local_image_validator or source_policy.check

    return AsyncQwen3VLReranker(
        base_url=settings.base_url,
        model_name=settings.model_name,
        api_key=settings.api_key_value or "EMPTY",
        image_url_validator=image_url_validator,
        local_image_validator=local_image_validator,
        max_local_image_bytes=config.limits.model_image_max_bytes,
    )


def _gated_routes(loader: AsyncAutoLoader) -> list[object]:
    """AsyncAutoLoader 自己不持有闸门，所以复验要落到真正发请求的叶子上。"""
    return [route.loader for route in loader.routes]


def _assert_gated(gate: Gate, *models: object) -> None:
    """启动时确认闸门已绑定。

    闸门是"静默失效"型的保护：没挂上不会报错、不会打日志，只是限流不生效 ——
    而那正是本项目实测出"配置写 4、实际 128"的那个缺陷。

    静态守卫（`tests/unit/test_layering.py`）能拦住仓库内直接 new 模型的写法，
    但拦不住**注入进来的自定义实现**：某个第三方子类把 `bind_gate` 覆写成空操作，
    静态检查看不见。所以这里在装配的最后一步再验一次 —— 宁可起不来，
    也别带着一个失效的闸门上线。
    """
    for model in models:
        if model is None:
            continue
        bound = getattr(model, "_gate", None)
        if bound is not gate:
            raise RuntimeError(
                f"{type(model).__name__} 没有正确挂上并发闸门"
                f"（bind_gate 被覆写了？）—— 限流会静默失效，拒绝启动"
            )


async def build_context(
    config: APPConfig,
    *,
    embedding_model: AsyncEmbeddingPort | None = None,
    reranker: AsyncRerankerPort | None = None,
    vector_store: AsyncVectorStore | None = None,
    task_store: TaskStore | None = None,
    task_executor: TaskExecutor | None = None,
    kb_repository: KnowledgeBaseRepository | None = None,
    document_store: DocumentStore | None = None,
    pipeline_config: PipelineConfig | None = None,
    ingest_loader: AsyncAutoLoader | None = None,
    mineru_extractor: AsyncDocumentExtractorPort | None = None,
    executor_lane: str | None = None,
    runners: RunnerRegistry | None = None,
) -> Context:
    """按配置装配全套资源。

    先校验，再创建。失败只回收本次自建资源；成功返回后 Context 接管包括
    注入对象在内的资源。闸门绑定会修改注入对象，失败不承诺恢复其原闸门。
    异步入口保证回滚在调用方的事件循环内完成，再传播原始启动错误。
    自带执行器与 runners 必须配对；注入自定义执行器时显式提供其消费者的注册表。
    """
    # APPConfig 只有配置数据；python 模式保留 SecretStr，且让嵌套配置重新校验。
    config = type(config).model_validate(config.model_dump(mode="python"))
    resolved_pipeline = resolve_pipeline_config(config.limits, pipeline_config)
    validate_vision_model(resolved_pipeline.docx.vision_model, asynchronous=True)
    if task_executor is not None:
        runners = _executor_runners(task_executor, runners)
    if config.retrieval.revision_aware and vector_store is not None:
        _validate_revision_vectors(vector_store)
    if document_store is not None:
        _validate_revision_resources(config, document_store, vector_store)
    needs_document_database = (
        config.retrieval.revision_aware
        and document_store is None
        and config.backends.vector_store is Backend.MILVUS
        and not isinstance(vector_store, AsyncInMemoryVectorStore)
    )
    needs_database = needs_document_database or (
        config.backends.task_store is Backend.POSTGRES
        and (task_store is None or kb_repository is None)
    )
    # 只要求实际需要创建的后端具备设置；显式注入仍可替换外部资源。
    settings = config.infrastructure_config
    if needs_database and settings.database is None:
        raise ValueError("选择了 postgres 后端但未配置 infrastructure_config.database")
    if vector_store is None:
        if config.backends.vector_store not in (Backend.MEMORY, Backend.MILVUS):
            raise ValueError(
                f"不支持的 vector_store 后端：{config.backends.vector_store}"
            )
        if (
            config.backends.vector_store is Backend.MILVUS
            and settings.vector_database is None
        ):
            raise ValueError(
                "backends.vector_store=milvus 但未配置 infrastructure_config.vector_database"
            )
    if (
        task_store is None or kb_repository is None
    ) and config.backends.task_store not in (Backend.MEMORY, Backend.POSTGRES):
        raise ValueError(f"不支持的 task_store 后端：{config.backends.task_store}")
    if task_executor is None:
        if config.backends.task_executor not in (Backend.INPROCESS, Backend.ARQ):
            raise ValueError(
                f"不支持的 task_executor 后端：{config.backends.task_executor}"
            )
        if config.backends.task_executor is Backend.ARQ and settings.redis is None:
            raise ValueError(
                "backends.task_executor=arq 但未配置 infrastructure_config.redis"
            )
    if ingest_loader is None and config.ingest_policy.allow_s3 and settings.s3 is None:
        raise ValueError(
            "ingest_policy.allow_s3=true 但未配置 infrastructure_config.s3"
        )

    source_policy = build_source_policy(config.ingest_policy)
    degradation = build_degradation(config)
    gate = build_model_gate(config, degradation)
    loader_gate = build_loader_gate(config)

    rollback = AsyncExitStack()
    try:
        database = build_database(config) if needs_database else None
        if database is not None:
            rollback.push_async_callback(close_resource, database)
        if config.retrieval.revision_aware and document_store is None:
            document_store = build_document_store(
                config, database, vector_store=vector_store
            )
            rollback.push_async_callback(close_resource, document_store)
            _validate_revision_resources(config, document_store, vector_store)

        if ingest_loader is None:
            ingest_loader = build_ingest_loader(config, source_policy)
            rollback.push_async_callback(close_resource, ingest_loader)
        if embedding_model is None:
            embedding_model = build_embedding_model(
                config,
                image_url_validator=source_policy.check_redirect,
                local_image_validator=source_policy.check,
            )
            rollback.push_async_callback(close_resource, embedding_model)
        if reranker is None:
            reranker = build_reranker(
                config,
                image_url_validator=source_policy.check_redirect,
                local_image_validator=source_policy.check,
            )
            if reranker is not None:
                rollback.push_async_callback(close_resource, reranker)

        # 闸门在这里挂上 —— 注入进来的替身同样要挂，否则测试跑的是"没有闸门"
        # 的那条路，而生产是另一条，两边行为不一致就白测了。
        embedding_model.bind_gate(gate)
        if reranker is not None:
            reranker.bind_gate(gate)
        _assert_gated(gate, embedding_model, reranker)

        # 加载侧的闸门。`AsyncAutoLoader` 只转发给叶子 loader，自己不持有 —— 它是
        # 路由器不是 I/O，持有会导致一次加载取两次许可（见 AsyncAutoLoader.bind_gate）。
        ingest_loader.bind_gate(loader_gate)
        _assert_gated(loader_gate, *_gated_routes(ingest_loader))

        if mineru_extractor is None:
            mineru_extractor = build_mineru_extractor(config)
            if mineru_extractor is not None:
                rollback.push_async_callback(close_resource, mineru_extractor)
        mineru_gate: Gate | None = None
        if mineru_extractor is not None:
            bind_gate = getattr(mineru_extractor, "bind_gate", None)
            if not callable(bind_gate):
                raise RuntimeError(
                    f"{type(mineru_extractor).__name__} 不支持进程级 MinerU 闸门"
                )
            mineru_gate = build_mineru_gate(config)
            bind_gate(mineru_gate)
            _assert_gated(mineru_gate, mineru_extractor)

        if vector_store is None:
            vector_store = build_vector_store(
                config, revision_aware=document_store is not None
            )
            rollback.push_async_callback(close_resource, vector_store)
        if task_store is None:
            task_store = build_task_store(config, database)
        if task_executor is None:
            task_executor = build_task_executor(
                config, task_store, lane=executor_lane, runners=runners
            )
            rollback.push_async_callback(
                run_cleanup, task_executor.shutdown, "task_executor"
            )
        runners = _executor_runners(task_executor, runners)
        if kb_repository is None:
            kb_repository = build_kb_repository(config, database)

        embedding_settings = config.infrastructure_config.embedding_model
        knowledge_base = KnowledgeBaseService(
            repository=kb_repository,
            vector_store=vector_store,
            embedding_model=embedding_settings.model_name,
            embedding_dim=embedding_settings.dim,
        )

        pipeline_hooks = AsyncPipelineHooks.fork()
        hook_cleanups: list[Callable[[], None]] = []
        rollback.push_async_callback(clear_registrations, hook_cleanups)
        if mineru_extractor is not None:
            hook_cleanups.append(
                wire_pdf_extractor(mineru_extractor, hooks=pipeline_hooks)
            )

        context = Context(
            embedding_model=embedding_model,
            reranker=reranker,
            vector_store=vector_store,
            task_store=task_store,
            task_executor=task_executor,
            task_service=TaskService(
                task_store, task_executor, max_backlog=config.limits.max_backlog
            ),
            retrieval=RetrievalService(
                embedding_model=embedding_model,
                vector_store=vector_store,
                keyword_search=(
                    vector_store
                    if isinstance(vector_store, AsyncKeywordSearchPort)
                    else None
                ),
                knowledge_base=knowledge_base,
                reranker=reranker,
                degradation=degradation,
                document_store=document_store,
                max_revision_candidates=config.retrieval.max_revision_candidates,
            ),
            kb_repository=kb_repository,
            knowledge_base=knowledge_base,
            embedding_dim=embedding_settings.dim,
            pipeline_hooks=pipeline_hooks,
            runners=runners,
            database=database,
            document_store=document_store,
            model_gate=gate,
            degradation=degradation,
            source_policy=source_policy,
            ingest_loader=ingest_loader,
            mineru_gate=mineru_gate,
            extracted_text_limits=(
                {"pdf": config.infrastructure_config.mineru.max_markdown_bytes}
                if mineru_extractor is not None
                else {}
            ),
            max_chunk_context_bytes=config.limits.task_chunk_context_max_bytes,
            _extra_closers=[
                ingest_loader,
                *([mineru_extractor] if mineru_extractor is not None else []),
            ],
            _hook_cleanups=hook_cleanups,
        )
        wire_runners(context, ingest_config=resolved_pipeline)

        logger.info(
            f"资源装配完成 vector_store={config.backends.vector_store} "
            f"task_store={config.backends.task_store} "
            f"executor={config.backends.task_executor} "
            f"rerank={'on' if reranker else 'off'}"
        )
        # 所有权只在完整装配后转交；成功路径由 Context 按依赖顺序关闭。
        rollback.pop_all()
        return context
    except BaseException:
        # 回滚与正常关停一样必须排干；取消启动不能留下清理到一半的连接池。
        await wait_for_cleanup(asyncio.create_task(rollback.aclose()))
        raise


__all__ = [
    "build_context",
    "build_document_store",
    "build_database",
    "build_embedding_model",
    "build_degradation",
    "build_model_gate",
    "build_kb_repository",
    "build_mineru_extractor",
    "build_mineru_gate",
    "build_reranker",
    "build_task_executor",
    "build_task_store",
    "build_vector_store",
    "wire_pdf_extractor",
]
