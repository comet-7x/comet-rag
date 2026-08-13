"""契约测试的等待原语。

**只允许通过 TaskStore 观察结果，绝不窥探执行器内部。**

这条纪律是契约能复用的前提：`InProcessExecutor` 有 `_bg` / `_detached`
可以 gather，而 `ArqExecutor` 的任务跑在别的进程里，根本没有本地协程可等。
把"任务跑完了没有"定义成"库里的状态到终态了没有"，两种实现才能跑同一套测试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from comet_rag.tasks import Task, TaskStore

DEFAULT_TIMEOUT = 5.0
POLL_INTERVAL = 0.005


async def wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    message: str = "条件在超时内未满足",
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(POLL_INTERVAL)
    raise AssertionError(message)


async def wait_for_terminal(
    store: TaskStore, task_id: str, *, timeout: float = DEFAULT_TIMEOUT
) -> Task:
    """等到任务落终态（SUCCEEDED / FAILED / CANCELLED）并返回它。

    注意"可重试失败"会让任务短暂回到 PENDING 再重跑，所以这里必须等**终态**，
    而不是等"不再是 RUNNING"。
    """

    async def done() -> bool:
        task = await store.get(task_id)
        return task is not None and task.status.is_terminal

    await wait_until(
        done, timeout=timeout, message=f"任务 {task_id} 在 {timeout}s 内未落终态"
    )
    return await store.require(task_id)


async def wait_for_status(
    store: TaskStore, task_id: str, status: str, *, timeout: float = DEFAULT_TIMEOUT
) -> Task:
    async def hit() -> bool:
        task = await store.get(task_id)
        return task is not None and task.status.value == status

    await wait_until(
        hit, timeout=timeout, message=f"任务 {task_id} 在 {timeout}s 内未进入 {status}"
    )
    return await store.require(task_id)
