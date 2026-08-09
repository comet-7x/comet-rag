"""应用生命周期管理"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

# from ..core.context import Context
from ..core.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """应用生命周期"""
    # 初始化日志
    setup_logging(
        module_files={
            "api": "api",
            "services": "services",
            "engines": "engines",
        }
    )

    # 初始化资源（TODO: 替换为实际资源创建）
    # from ..infrastructure.models.llm import ModelFactory
    # from ..infrastructure.models.embedding import Qwen3VLEmbeddingModel
    # from ..infrastructure.models.reranker import Qwen3VLReranker
    # from ..infrastructure.vectorstore import MilvusStorage
    #
    # llm = ModelFactory.create(...)
    # embedding = Qwen3VLEmbeddingModel(...)
    # reranker = Qwen3VLReranker(...)
    # vectorstore = MilvusStorage(...)

    # 临时空 Context，后续填充
    # ctx = Context()
    # app.state.ctx = ctx

    yield

    # 清理资源
    # await vectorstore.close()
