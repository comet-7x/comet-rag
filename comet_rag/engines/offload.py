from __future__ import annotations

import asyncio
from collections.abc import Callable


async def run_sync[**P, R](
    function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    """同步工作不可取消；等待线程结束后才传播取消，防止调用方提前释放输入资源。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break
    if cancelled is not None:
        # 消费线程错误，避免在已取消请求之外遗留未取回的异常。
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()
