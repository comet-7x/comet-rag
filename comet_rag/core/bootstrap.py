"""组合根：唯一知道"用哪个实现"的地方。

本模块是刻意"向下"依赖 services / infrastructure 的 —— 组合根不属于任何
一层，它坐在所有层之上，负责把接口与实现拼起来。除了它，任何模块都不该
出现 `if backend == "milvus"` 这类分支：那意味着实现选择泄漏进了业务代码。

API 进程与 worker 进程共用这里的装配逻辑，只是各自用到的部分不同 ——
worker 不需要 FastAPI，但同样需要 runner 与全套资源。
"""

from __future__ import annotations

from comet_rag.config.schemas import APPConfig, Backend
from comet_rag.core.concurrency import Gate, build_gate
from comet_rag.core.context import Context, wire_runners
from comet_rag.core.logging import logger
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
    KnowledgeBaseRepository,
)
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.models.reranker.base import BaseReranker
from comet_rag.infrastructure.vectorstore import (
    BaseVectorStore,
    InMemoryVectorStore,
)
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.retrieval import RetrievalService
from comet_rag.tasks import (
    LANE_CPU,
    InMemoryTaskStore,
    InProcessExecutor,
    TaskExecutor,
    TaskService,
    TaskStore,
)


def build_vector_store(config: APPConfig) -> BaseVectorStore:
    backend = config.backends.vector_store
    if backend is Backend.MEMORY:
        return InMemoryVectorStore()
    if backend is Backend.MILVUS:
        # pymilvus 在 `milvus` extra 里，函数内 import 才不会拖累未安装的用户
        from comet_rag.infrastructure.vectorstore.milvus import (  # noqa: PLC0415
            MilvusStore,
        )

        settings = config.infrastructure_config.vector_database
        if settings is None:
            raise ValueError(
                "backends.vector_store=milvus 但未配置 "
                "infrastructure_config.vector_database"
            )
        return MilvusStore(
            endpoint=settings.endpoint,
            api_key=settings.api_key,
        )
    raise ValueError(f"不支持的 vector_store 后端：{backend}")


def build_database(config: APPConfig):
    """关系库引擎。只在真的需要时创建 —— 全 memory 的部署不该被迫连库。"""
    from comet_rag.infrastructure.database import Database  # noqa: PLC0415

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
    from comet_rag.infrastructure.database.kb_repository import (  # noqa: PLC0415
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
        from comet_rag.tasks.store_postgres import (  # noqa: PLC0415
            PostgresTaskStore,
        )

        if database is None:
            raise ValueError("postgres 后端需要传入 database")
        return PostgresTaskStore(database)
    raise ValueError(f"不支持的 task_store 后端：{backend}")


def build_task_executor(
    config: APPConfig, store: TaskStore, *, lane: str | None = None
) -> TaskExecutor:
    """`lane` = 本进程服务哪条负载道。

    API 进程与单进程部署都传 None（不分道，只作生产端）；worker 进程由
    `workers/base.py` 按自己的画像传 cpu / io。
    """
    backend = config.backends.task_executor
    if backend is Backend.INPROCESS:
        # 单进程下分道毫无意义：移交只会变成一次没必要的入队往返，
        # 而且 InProcessExecutor 压根没有"另一条队列"可投。
        return InProcessExecutor(store, max_concurrency=config.backends.max_concurrency)
    if backend is Backend.ARQ:
        from comet_rag.tasks.executor_arq import (  # noqa: PLC0415
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
        )
    raise ValueError(f"不支持的 task_executor 后端：{backend}")


def build_model_gate(config: APPConfig) -> Gate:
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
    )


def build_embedding_model(config: APPConfig) -> BaseEmbeddingModel:
    from comet_rag.infrastructure.models.embedding.qwen3_vl_embedding import (  # noqa: PLC0415
        Qwen3VLEmbeddingModel,
    )

    settings = config.infrastructure_config.embedding_model
    return Qwen3VLEmbeddingModel(
        base_url=settings.base_url,
        model_name=settings.model_name,
        api_key=settings.api_key or "EMPTY",
    )


def build_reranker(config: APPConfig) -> BaseReranker | None:
    settings = config.infrastructure_config.reranker
    if settings is None:
        logger.info("未配置 reranker，检索将跳过重排")
        return None
    from comet_rag.infrastructure.models.reranker.qwen3_vl_reranker import (  # noqa: PLC0415
        Qwen3VLReranker,
    )

    return Qwen3VLReranker(
        base_url=settings.base_url,
        model_name=settings.model_name,
        api_key=settings.api_key or "EMPTY",
    )


def build_context(
    config: APPConfig,
    *,
    embedding_model: BaseEmbeddingModel | None = None,
    reranker: BaseReranker | None = None,
    vector_store: BaseVectorStore | None = None,
    task_store: TaskStore | None = None,
    task_executor: TaskExecutor | None = None,
    kb_repository: KnowledgeBaseRepository | None = None,
    pipeline_config: PipelineConfig | None = None,
    executor_lane: str | None = None,
) -> Context:
    """按配置装配全套资源。

    每个依赖都可被显式传入覆盖 —— 测试要注入假模型，`build_embedding_model`
    却会去连真实服务。有了这些参数，测试无需 monkeypatch 就能用同一条装配路径，
    也就真正测到了装配逻辑本身。
    """
    needs_database = config.backends.task_store is Backend.POSTGRES and (
        task_store is None or kb_repository is None
    )
    database = build_database(config) if needs_database else None

    embedding_model = embedding_model or build_embedding_model(config)
    reranker = reranker if reranker is not None else build_reranker(config)

    # 闸门在这里挂上 —— 注入进来的替身同样要挂，否则测试跑的是"没有闸门"
    # 的那条路，而生产是另一条，两边行为不一致就白测了。
    gate = build_model_gate(config)
    embedding_model.bind_gate(gate)
    if reranker is not None:
        reranker.bind_gate(gate)
    vector_store = vector_store or build_vector_store(config)
    task_store = task_store or build_task_store(config, database)
    task_executor = task_executor or build_task_executor(
        config, task_store, lane=executor_lane
    )
    kb_repository = kb_repository or build_kb_repository(config, database)

    embedding_settings = config.infrastructure_config.embedding_model
    knowledge_base = KnowledgeBaseService(
        repository=kb_repository,
        vector_store=vector_store,
        embedding_model=embedding_settings.model_name,
        embedding_dim=embedding_settings.dim,
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
            reranker=reranker,
        ),
        kb_repository=kb_repository,
        knowledge_base=knowledge_base,
        embedding_dim=embedding_settings.dim,
        database=database,
        model_gate=gate,
    )
    wire_runners(context, ingest_config=pipeline_config)

    logger.info(
        f"资源装配完成 vector_store={config.backends.vector_store} "
        f"task_store={config.backends.task_store} "
        f"executor={config.backends.task_executor} "
        f"rerank={'on' if reranker else 'off'}"
    )
    return context


__all__ = [
    "build_context",
    "build_database",
    "build_embedding_model",
    "build_model_gate",
    "build_kb_repository",
    "build_reranker",
    "build_task_executor",
    "build_task_store",
    "build_vector_store",
]
