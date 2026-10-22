from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import AsyncExitStack

from comet_rag.core.logging import logger


async def wait_for_cleanup(task: asyncio.Task[None]) -> None:
    """调用方取消不能中断资源排干；清理完成后再传播取消（包括重复取消）。"""
    interrupted: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.done():
                raise
            interrupted = exc
    if interrupted is not None:
        raise interrupted


async def run_cleanup(cleanup: Callable[[], object], label: str) -> None:
    """调用和等待都在保护内；一个资源的关闭错误不能阻断其余清理。"""
    try:
        result = cleanup()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — 清理失败只记录，保留启动的原始错误
        logger.warning(f"关闭 {label} 时出错（继续释放其余资源）：{exc!r}")


async def close_resource(resource: object) -> None:
    """组合根管理的资源统一提供 close；执行模型由资源类型决定。"""
    method = getattr(resource, "close", None)
    if callable(method):
        await run_cleanup(method, type(resource).__name__)


async def clear_registrations(cleanups: list[Callable[[], None]]) -> None:
    """列表在装配期间可追加；失败回滚和正常关闭都撤销最终登记的引用。"""
    async with AsyncExitStack() as closing:
        for cleanup in cleanups:
            closing.push_async_callback(run_cleanup, cleanup, "注册引用")
        cleanups.clear()
