"""应用上下文：进程内所有长生命周期资源的唯一持有者。

## 为什么需要它

httpx 连接池、向量库连接、任务执行器都是**应用级单例**：现建现销会重建
连接池和 TLS 握手（spec S4-4），而散落在各处 new 出来则没人知道该在何时关闭。
`Context` 把它们收拢成一个对象，创建与释放各只有一处。

## 关停必须逆序

依赖是有方向的：executor 在跑的任务会用 vector_store，vector_store 可能
在用 httpx client。先关 client 再等 executor 收尾，在跑的任务就会撞上
"连接已关闭"。所以 `aclose()` 严格按创建的**逆序**释放，且**先停执行器**
——让在途任务先落到一致状态，再拆它们脚下的地板。

## 后端由配置决定

同一份装配代码既能跑纯内存（测试、开发、Phase 3 的端到端验证），
也能跑真实中间件（Phase 4 起）。这是 plan"先内存后真实"能成立的关键：
换后端只改配置，业务代码一行不动。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from comet_rag.application.ports import EmbeddingPort, RerankerPort
from comet_rag.core.logging import logger
from comet_rag.engines.loaders.auto_loader import AutoLoader
from comet_rag.infrastructure.knowledge_base import KnowledgeBaseRepository
from comet_rag.infrastructure.vectorstore import BaseVectorStore
from comet_rag.services.ingestion import IngestRunner, register_ingest_runner
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.retrieval import RetrievalService
from comet_rag.tasks import TaskExecutor, TaskService, TaskStore


@dataclass(slots=True)
class Context:
    """长生命周期资源。由 `build_context()` 创建，`aclose()` 释放。

    路由只从这里取依赖，绝不自己 new —— 否则连接池复用与优雅关停都无从谈起。
    """

    embedding_model: EmbeddingPort
    vector_store: BaseVectorStore
    task_store: TaskStore
    task_executor: TaskExecutor
    task_service: TaskService
    retrieval: RetrievalService
    kb_repository: KnowledgeBaseRepository
    knowledge_base: KnowledgeBaseService
    embedding_dim: int
    reranker: RerankerPort | None = None
    #: 对模型服务的进程级并发闸门。放进 Context 是为了让 /admin/health
    #: 能读到它的实时统计 —— 过载时最先想看的就是这几个数。
    model_gate: Any = None
    #: 分级降级控制器（S4-5）。读路径与写路径都要问它。
    degradation: Any = None
    #: 入库来源准入策略。写路径在**建任务之前**问它。
    source_policy: Any = None
    #: 组合根装配的来源路由器；包含可选的基础设施 Loader。
    ingest_loader: AutoLoader | None = None
    #: 仅在 task_store/kb 用 postgres 时存在。关停时要 dispose 连接池。
    database: Any = None
    #: 需要在关停时释放、但不属于上面任何一类的资源（按注册顺序逆序关闭）
    _extra_closers: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        """逆序释放。任一步失败都不得中断其余释放 —— 否则一个坏掉的连接
        会让整个进程留下一堆泄漏的资源。"""
        # 1. 先停执行器：让在途任务落到一致状态，再拆它们脚下的地板
        await _safe(self.task_executor.shutdown(), "task_executor")
        # 2. 再关它们用到的下游
        await _safe(self.vector_store.aclose(), "vector_store")
        if self.reranker is not None:
            await _safe(_maybe_close(self.reranker), "reranker")
        await _safe(_maybe_close(self.embedding_model), "embedding_model")
        # 数据库放最后：上面几步失败时的错误处理可能还要读写任务状态
        if self.database is not None:
            await _safe(_maybe_close(self.database), "database")
        for closer in reversed(self._extra_closers):
            await _safe(_maybe_close(closer), type(closer).__name__)


async def _maybe_close(resource: Any) -> None:
    """使用统一的 ``aclose``，并兼容旧资源的关闭方法名。

    ``close_client`` 暂时排在前面：旧的自定义模型可能继承了基类新增的空
    ``aclose``，但只覆写旧方法；先命中旧方法才能在迁移期正确释放资源。
    """
    for name in ("close_client", "aclose", "close"):
        method = getattr(resource, name, None)
        if callable(method):
            result = method()
            # `inspect.isawaitable` 而非 `hasattr(result, "__await__")`：
            # 前者同时覆盖协程与 Future，且是类型检查器认得的收窄。
            if inspect.isawaitable(result):
                await result
            return


async def _safe(awaitable: Any, label: str) -> None:
    try:
        if inspect.isawaitable(awaitable):
            await awaitable
    except Exception as exc:  # noqa: BLE001 —— 关停期间任何失败都只记录
        logger.warning(f"关闭 {label} 时出错（已忽略，继续释放其余资源）：{exc!r}")


def wire_runners(context: Context, *, ingest_config: Any = None) -> None:
    """把业务 runner 绑到任务框架上。

    单独成函数而非塞进 `build_context`：worker 进程也要调它（它需要 runner，
    但不需要 FastAPI），而 API 进程调它是为了让"提交后本进程也能执行"
    在单进程模式下成立。
    """
    loader = context.ingest_loader
    if loader is None:
        policy = context.source_policy
        loader = AutoLoader.default(
            max_download_bytes=getattr(policy, "max_download_bytes", None),
            redirect_validator=(policy.check_redirect if policy is not None else None),
        )
        context.ingest_loader = loader
        context._extra_closers.append(loader)
    register_ingest_runner(
        IngestRunner(
            embedding_model=context.embedding_model,
            vector_store=context.vector_store,
            knowledge_base=context.knowledge_base,
            loader=loader,
            config=ingest_config,
        )
    )
