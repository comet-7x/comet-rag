"""持有进程级共享资源，并按依赖逆序关闭。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from comet_rag.composition.resources import (
    close_resource,
    run_cleanup,
    wait_for_cleanup,
)
from comet_rag.engines.pipelines import AsyncHookRegistry
from comet_rag.infrastructure.sources import AsyncAutoLoader
from comet_rag.ports import AsyncEmbeddingPort, AsyncRerankerPort, AsyncVectorStore
from comet_rag.ports.document_store import DocumentStore
from comet_rag.ports.knowledge_base import KnowledgeBaseRepository
from comet_rag.services.ingestion import (
    DEFAULT_MAX_CHUNK_CONTEXT_BYTES,
    INGEST_KIND,
    IngestRunner,
    register_ingest_runner,
)
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.retrieval import RetrievalService
from comet_rag.tasks import TaskExecutor, TaskService, TaskStore
from comet_rag.tasks.runner import unregister


@dataclass(slots=True)
class Context:
    """长生命周期资源。由 `await build_context()` 创建，`close()` 释放。

    路由只从这里取依赖，绝不自己 new —— 否则连接池复用与优雅关停都无从谈起。
    """

    embedding_model: AsyncEmbeddingPort
    vector_store: AsyncVectorStore
    task_store: TaskStore
    task_executor: TaskExecutor
    task_service: TaskService
    retrieval: RetrievalService
    kb_repository: KnowledgeBaseRepository
    knowledge_base: KnowledgeBaseService
    embedding_dim: int
    pipeline_hooks: AsyncHookRegistry
    reranker: AsyncRerankerPort | None = None
    document_store: DocumentStore | None = None
    #: 对模型服务的进程级并发闸门。放进 Context 是为了让 /admin/health
    #: 能读到它的实时统计 —— 过载时最先想看的就是这几个数。
    model_gate: Any = None
    #: 分级降级控制器（S4-5）。读路径与写路径都要问它。
    degradation: Any = None
    #: 入库来源准入策略。写路径在**建任务之前**问它。
    source_policy: Any = None
    #: 组合根装配的来源路由器；包含可选的基础设施 Loader。
    ingest_loader: AsyncAutoLoader | None = None
    #: MinerU 使用独立预算；None 表示未启用 PDF 外部提取。
    mineru_gate: Any = None
    #: 写入 Task context 前按文件类型执行的第二道文本字节限制。
    extracted_text_limits: dict[str, int] = field(default_factory=dict)
    #: ChunkDraft 在 CPU/IO worker 之间交接时的 JSON payload 上限。
    max_chunk_context_bytes: int = DEFAULT_MAX_CHUNK_CONTEXT_BYTES
    #: 仅在 task_store/kb 用 postgres 时存在。关停时要 dispose 连接池。
    database: Any = None
    #: 需要在关停时释放、但不属于上面任何一类的资源（按注册顺序逆序关闭）
    _extra_closers: list[Any] = field(default_factory=list)
    #: 先撤销引用外围资源的注册，再真正关闭资源。
    _hook_cleanups: list[Callable[[], None]] = field(default_factory=list)

    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def close(self) -> None:
        """先排干任务再关闭依赖；并发调用共享清理，取消也要等排干完成。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await wait_for_cleanup(self._close_task)

    async def _close_resources(self) -> None:
        # 按逆序执行；一个资源自身抛出关闭错误也会尝试其余清理。
        async with AsyncExitStack() as closing:
            if self.database is not None:
                closing.push_async_callback(close_resource, self.database)
            closing.push_async_callback(close_resource, self.embedding_model)
            if self.reranker is not None:
                closing.push_async_callback(close_resource, self.reranker)
            if self.document_store is not None:
                closing.push_async_callback(close_resource, self.document_store)
            closing.push_async_callback(close_resource, self.vector_store)
            for resource in self._extra_closers:
                closing.push_async_callback(close_resource, resource)
            for cleanup in self._hook_cleanups:
                closing.push_async_callback(run_cleanup, cleanup, "注册引用")
            closing.push_async_callback(
                run_cleanup, self.task_executor.shutdown, "task_executor"
            )
            self._hook_cleanups.clear()
            self._extra_closers.clear()


def wire_runners(context: Context, *, ingest_config: Any = None) -> None:
    """把业务 runner 绑到任务框架上。

    单独成函数而非塞进 `build_context`：worker 进程也要调它（它需要 runner，
    但不需要 FastAPI），而 API 进程调它是为了让"提交后本进程也能执行"
    在单进程模式下成立。
    """
    loader = context.ingest_loader
    if loader is None:
        policy = context.source_policy
        loader = AsyncAutoLoader.default(
            max_download_bytes=getattr(policy, "max_download_bytes", None),
            redirect_validator=(policy.check_redirect if policy is not None else None),
        )
        context.ingest_loader = loader
        context._extra_closers.append(loader)
    runner = IngestRunner(
        embedding_model=context.embedding_model,
        vector_store=context.vector_store,
        knowledge_base=context.knowledge_base,
        loader=loader,
        config=ingest_config,
        hooks=context.pipeline_hooks,
        max_extracted_text_bytes_by_type=context.extracted_text_limits,
        max_chunk_context_bytes=context.max_chunk_context_bytes,
    )
    context._hook_cleanups.append(lambda: unregister(INGEST_KIND, expected=runner))
    register_ingest_runner(runner)
