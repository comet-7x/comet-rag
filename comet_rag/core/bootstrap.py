"""组合根：唯一知道"用哪个实现"的地方。

本模块是刻意"向下"依赖 services / infrastructure 的 —— 组合根不属于任何
一层，它坐在所有层之上，负责把接口与实现拼起来。除了它，任何模块都不该
出现 `if backend == "milvus"` 这类分支：那意味着实现选择泄漏进了业务代码。

API 进程与 worker 进程共用这里的装配逻辑，只是各自用到的部分不同 ——
worker 不需要 FastAPI，但同样需要 runner 与全套资源。
"""

from __future__ import annotations

from comet_rag.config.schemas import APPConfig, Backend
from comet_rag.core.context import Context, wire_runners
from comet_rag.core.logging import logger
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.models.reranker.base import BaseReranker
from comet_rag.infrastructure.vectorstore import (
    BaseVectorStore,
    InMemoryVectorStore,
)
from comet_rag.services.retrieval import RetrievalService
from comet_rag.tasks import (
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


def build_task_store(config: APPConfig) -> TaskStore:
    backend = config.backends.task_store
    if backend is Backend.MEMORY:
        return InMemoryTaskStore()
    if backend is Backend.POSTGRES:
        from comet_rag.tasks.store_postgres import (  # noqa: PLC0415
            PostgresTaskStore,
        )

        return PostgresTaskStore()
    raise ValueError(f"不支持的 task_store 后端：{backend}")


def build_task_executor(config: APPConfig, store: TaskStore) -> TaskExecutor:
    backend = config.backends.task_executor
    if backend is Backend.INPROCESS:
        return InProcessExecutor(store, max_concurrency=config.backends.max_concurrency)
    if backend is Backend.ARQ:
        from comet_rag.tasks.executor_arq import ArqExecutor  # noqa: PLC0415

        return ArqExecutor(store)
    raise ValueError(f"不支持的 task_executor 后端：{backend}")


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
    pipeline_config: PipelineConfig | None = None,
) -> Context:
    """按配置装配全套资源。

    每个依赖都可被显式传入覆盖 —— 测试要注入假模型，`build_embedding_model`
    却会去连真实服务。有了这些参数，测试无需 monkeypatch 就能用同一条装配路径，
    也就真正测到了装配逻辑本身。
    """
    embedding_model = embedding_model or build_embedding_model(config)
    reranker = reranker if reranker is not None else build_reranker(config)
    vector_store = vector_store or build_vector_store(config)
    task_store = task_store or build_task_store(config)
    task_executor = task_executor or build_task_executor(config, task_store)

    context = Context(
        embedding_model=embedding_model,
        reranker=reranker,
        vector_store=vector_store,
        task_store=task_store,
        task_executor=task_executor,
        task_service=TaskService(task_store, task_executor),
        retrieval=RetrievalService(
            embedding_model=embedding_model,
            vector_store=vector_store,
            reranker=reranker,
        ),
        embedding_dim=config.infrastructure_config.embedding_model.dim,
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
    "build_embedding_model",
    "build_reranker",
    "build_task_executor",
    "build_task_store",
    "build_vector_store",
]
