"""请求级依赖注入。

**路由不得自己 new 资源** —— 全部经此处从 `app.state.ctx` 取。
否则每个请求都会新建连接池（spec S4-4），关停时也没人知道该释放什么。
"""

from typing import Annotated

from fastapi import Depends, Request

from ..core.context import Context
from ..infrastructure.vectorstore import BaseVectorStore
from ..services.retrieval import RetrievalService
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


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
RetrievalDep = Annotated[RetrievalService, Depends(get_retrieval)]
VectorStoreDep = Annotated[BaseVectorStore, Depends(get_vector_store)]
