"""请求级依赖注入。

**路由不得自己 new 资源** —— 全部经此处从 `app.state.ctx` 取。
否则每个请求都会新建连接池（spec S4-4），关停时也没人知道该释放什么。
"""

from typing import Annotated

from fastapi import Depends, Request

from ..core.concurrency import Overloaded
from ..core.context import Context
from ..core.degradation import Level
from ..infrastructure.vectorstore import BaseVectorStore
from ..services.knowledge_base import KnowledgeBaseService
from ..services.retrieval import RetrievalService
from ..services.source_policy import SourcePolicy
from ..tasks import TaskService


def get_context(request: Request) -> Context:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - 只会在 lifespan 未跑时出现
        raise RuntimeError("应用上下文未初始化：lifespan 未执行或已关停")
    return ctx


ContextDep = Annotated[Context, Depends(get_context)]


def get_task_service(ctx: ContextDep) -> TaskService:
    return ctx.task_service


def get_retrieval(ctx: ContextDep) -> RetrievalService:
    return ctx.retrieval


def get_vector_store(ctx: ContextDep) -> BaseVectorStore:
    return ctx.vector_store


def get_knowledge_base(ctx: ContextDep) -> KnowledgeBaseService:
    return ctx.knowledge_base


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
RetrievalDep = Annotated[RetrievalService, Depends(get_retrieval)]
VectorStoreDep = Annotated[BaseVectorStore, Depends(get_vector_store)]
KnowledgeBaseDep = Annotated[KnowledgeBaseService, Depends(get_knowledge_base)]


def admission_guard(ctx: ContextDep) -> None:
    """降级的**最后一级**：拒绝新的写入任务（spec S4-5）。

    放在 API 层而不是 `TaskService` 里，有两个理由：

    · `comet_rag/tasks/` 是与产品无关的通用框架，不该认识"降级"这个概念；
    · "收不收这个请求"本来就是入口的职责 —— 读路径不受影响，正是这一级
      想保住的东西。

    顺带一提，闸门的饱和度也在这里报给控制器：每个写请求都会经过它，
    采样频率天然跟着负载走，负载越高看得越勤。
    """
    degradation = getattr(ctx, "degradation", None)
    if degradation is None:
        return
    gate = getattr(ctx, "model_gate", None)
    if gate is not None:
        stats = gate.stats
        degradation.observe_saturation(stats.waiting, stats.limit)
    if not degradation.accept_writes():
        raise Overloaded(
            "degradation",
            f"服务已降级至 {Level.REJECT_WRITES.name}，暂不受理新的入库任务",
        )


AdmissionDep = Annotated[None, Depends(admission_guard)]


def get_source_policy(ctx: ContextDep) -> SourcePolicy | None:
    return getattr(ctx, "source_policy", None)


SourcePolicyDep = Annotated["SourcePolicy | None", Depends(get_source_policy)]
