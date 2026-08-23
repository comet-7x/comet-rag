"""应用生命周期：启动时装配资源，关停时逆序释放。"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from ..composition.bootstrap import build_context
from ..config.schemas import APPConfig
from ..core.logging import setup_logging


def make_lifespan(config: APPConfig, **build_kwargs: Any) -> Callable[[FastAPI], Any]:
    """产出一个绑定了配置的 lifespan。

    `build_kwargs` 直接透传给 `build_context`，端到端测试借此注入假模型与
    内存后端 —— 于是测试走的是**真实装配路径**，而不是另抄一份接线代码。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        setup_logging(
            module_files={
                "api": "api",
                "services": "services",
                "engines": "engines",
            }
        )

        context = build_context(config, **build_kwargs)
        app.state.ctx = context
        try:
            yield
        finally:
            # 即便启动后发生异常也要走到这里，否则连接池会随进程一起泄漏
            await context.aclose()

    return lifespan
