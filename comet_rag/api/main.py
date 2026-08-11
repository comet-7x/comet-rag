"""FastAPI 应用入口。

**领域异常到 HTTP 状态码的映射集中在这里**，路由里不散落 `HTTPException`。
理由：同一个 `TaskNotFound` 在三个路由里各写一次 404，迟早有一处漏掉或写错；
而且 services 层不该为了 HTTP 语义去 import fastapi。
"""

from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse

from comet_rag.api.lifespan import make_lifespan
from comet_rag.api.middleware import TraceMiddleware, get_trace_id
from comet_rag.api.routes import admin, ingest, kb, search, tasks
from comet_rag.config.schemas import APPConfig
from comet_rag.config.settings import get_config
from comet_rag.infrastructure.knowledge_base import (
    EmbeddingModelChanged,
    KnowledgeBaseExists,
    KnowledgeBaseNotFound,
)
from comet_rag.infrastructure.vectorstore import CollectionNotFound, DimensionMismatch
from comet_rag.tasks import TaskBusy, TaskNotFound, VersionConflict


def _problem(request: Request, code: int, message: str) -> JSONResponse:
    """带上 trace_id —— 用户报错时能直接对上日志，省掉一轮来回。"""
    return JSONResponse(
        status_code=code,
        content={
            "error": message,
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(TaskNotFound)
    async def _task_not_found(request: Request, exc: TaskNotFound) -> JSONResponse:  # noqa: RUF029
        return _problem(request, status.HTTP_404_NOT_FOUND, f"任务不存在：{exc!s}")

    @app.exception_handler(CollectionNotFound)
    async def _kb_not_found(request: Request, exc: CollectionNotFound) -> JSONResponse:  # noqa: RUF029
        return _problem(request, status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(VersionConflict)
    async def _conflict(request: Request, exc: VersionConflict) -> JSONResponse:  # noqa: RUF029
        """409：期间有别人写过这条记录，客户端重读后重试即可。"""
        return _problem(request, status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(DimensionMismatch)
    async def _dim(request: Request, exc: DimensionMismatch) -> JSONResponse:  # noqa: RUF029
        """409 而非 400：请求本身没错，是知识库已有的维度与当前模型对不上。"""
        return _problem(request, status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(KnowledgeBaseNotFound)
    async def _kb_missing(request: Request, exc: KnowledgeBaseNotFound) -> JSONResponse:  # noqa: RUF029
        return _problem(request, status.HTTP_404_NOT_FOUND, str(exc))

    @app.exception_handler(KnowledgeBaseExists)
    async def _kb_exists(request: Request, exc: KnowledgeBaseExists) -> JSONResponse:  # noqa: RUF029
        return _problem(request, status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(EmbeddingModelChanged)
    async def _model_changed(
        request: Request, exc: EmbeddingModelChanged
    ) -> JSONResponse:  # noqa: RUF029
        """409：请求没错，是这个知识库当初用的模型和现在配的不是一个。"""
        return _problem(request, status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(TaskBusy)
    async def _busy(request: Request, exc: TaskBusy) -> JSONResponse:  # noqa: RUF029
        return _problem(request, status.HTTP_409_CONFLICT, str(exc))

    @app.exception_handler(ValueError)
    async def _bad_request(request: Request, exc: ValueError) -> JSONResponse:  # noqa: RUF029
        """兜底：services 层用 ValueError 表达"你请求得不对"（如只有 FAILED 可重试）。"""
        return _problem(request, status.HTTP_400_BAD_REQUEST, str(exc))


def create_app(config: APPConfig | None = None, **lifespan_kwargs: Any) -> FastAPI:
    """应用工厂。

    做成工厂而非模块级单例：模块级 `get_config()` 会让"import 这个模块"
    等价于"必须存在一份合法配置"，测试、CI、以及任何只想 import 一下的场景
    都被绑架。工厂还让端到端测试能注入内存后端，从而测到**真实的装配路径**，
    而不是在测试里另抄一份。
    """
    config = config or get_config()

    app = FastAPI(
        title=config.server_config.app_name,
        lifespan=make_lifespan(config, **lifespan_kwargs),
        dependencies=[Depends(get_trace_id)],
    )

    app.include_router(ingest.router)
    app.include_router(tasks.router)
    app.include_router(search.router)
    app.include_router(kb.router)
    app.include_router(admin.router)
    app.add_middleware(TraceMiddleware)

    _install_exception_handlers(app)

    @app.get("/")
    async def root() -> dict[str, str]:  # noqa: RUF029
        return {"message": "Comet-RAG API"}

    return app


def __getattr__(name: str) -> Any:
    """让 `comet_rag.api.main:app` 可用，但**只在真正取用时**才读配置。

    写成模块级 `app = create_app()` 的话，"import 这个模块"就等价于
    "必须存在一份合法配置" —— 文档守卫、静态检查、任何只想看一眼的工具
    都会被一份缺字段的 config.yaml 拦住。PEP 562 的模块级 __getattr__
    把这个代价推迟到 uvicorn 真正来取 `app` 的那一刻。
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    app = create_app()
    _config = get_config()
    uvicorn.run(app, host=_config.server_config.host, port=_config.server_config.port)
