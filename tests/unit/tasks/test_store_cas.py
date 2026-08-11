"""`TaskStore._cas` 的重读重试策略。

这段行为是 T22 的集成测试逼出来的：跨进程部署下，runner 一边在
`enter_stage`，另一边有人请求取消，两次写入撞在同一个版本号上，
取消直接抛 `VersionConflict` 失败了 —— 任务永远停不下来。

集成测试能复现（PostgreSQL + 真 Redis 下窗口宽到必撞），但它依赖时序。
这里用**注入一次冲突**的方式把同一件事测成确定的：不需要中间件，
也不会因为机器快慢而时灵时不灵。
"""

from __future__ import annotations

import pytest

from comet_rag.tasks import (
    InMemoryTaskStore,
    Task,
    TaskStatus,
    VersionConflict,
)
from comet_rag.tasks.store import _CAS_RETRIES


class _RacyStore(InMemoryTaskStore):
    """在前 `n` 次 `_save` 之前，替一个"并发写入者"把版本号往前推一格。

    模拟的是真实场景：读出快照之后、写回之前，别人抢先写了一次。
    """

    def __init__(self, conflicts: int) -> None:
        super().__init__()
        self.remaining = conflicts
        self.save_calls = 0

    async def _save(self, task: Task, expected_version: int, *, bump: bool = True):
        self.save_calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            async with self._lock:  # 绕开 CAS 直接改，等价于"别人写过了"
                current = self._tasks[task.task_id]
                current.version += 1
                current.message = "别人抢先写的"
        return await super()._save(task, expected_version, bump=bump)


async def test_transition_retries_when_someone_else_wrote_first() -> None:
    """没传 expected_version = 调用方没做版本假设，撞了就该重读重来。"""
    store = _RacyStore(conflicts=1)
    task = await store.create("demo")

    moved = await store.transition(task.task_id, TaskStatus.RUNNING)

    assert moved.status is TaskStatus.RUNNING
    assert store.save_calls == 2, "没有重试 —— 一次就成功说明冲突根本没注入进去"
    assert store.remaining == 0


async def test_update_retries_too() -> None:
    store = _RacyStore(conflicts=1)
    task = await store.create("demo")

    updated = await store.update(task.task_id, message="我的写入")

    assert updated.message == "我的写入", "重试后把别人的写入正确覆盖了吗"
    assert store.save_calls == 2


async def test_enter_stage_retries_too() -> None:
    """取消与阶段推进撞车正是线上那个 bug 的现场，两边都得能重试。"""
    store = _RacyStore(conflicts=1)
    task = await store.create("demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    store.remaining = 1  # transition 那次已消耗掉，给 enter_stage 再注一次

    staged = await store.enter_stage(task.task_id, "extracting")

    assert staged.stage == "extracting"
    assert [r.stage for r in staged.stage_history] == ["extracting"]


async def test_heartbeat_retries_without_bumping_version() -> None:
    """心跳撞版本也要能续上；但重试后仍**不得**涨版本号 ——
    涨了就会把所有 runner 手里的 expected_version 撞失效（见 `_save` 的注释）。"""
    store = _RacyStore(conflicts=1)
    task = await store.create("demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    before = (await store.require(task.task_id)).version
    store.remaining = 1

    await store.heartbeat(task.task_id)

    after = await store.require(task.task_id)
    assert after.heartbeat_at is not None
    # 注入的那次冲突本身涨了 1 格，心跳自己一格都不许涨
    assert after.version == before + 1


async def test_explicit_expected_version_is_never_retried() -> None:
    """调用方显式传版本号 = 在断言"我要的就是这一版"。

    替它重试就等于悄悄覆盖别人的写入 —— 那正是乐观锁要防的事。
    """
    store = _RacyStore(conflicts=1)
    task = await store.create("demo")
    version = (await store.require(task.task_id)).version

    with pytest.raises(VersionConflict):
        await store.update(task.task_id, message="我", expected_version=version)

    assert store.save_calls == 1, "显式版本号的冲突被重试了"


async def test_retries_are_bounded() -> None:
    """持续冲突不能变成无限重试 —— 那会把一次故障拖成整个进程挂死。"""
    store = _RacyStore(conflicts=_CAS_RETRIES + 5)
    task = await store.create("demo")

    with pytest.raises(VersionConflict):
        await store.update(task.task_id, message="永远撞车")

    assert store.save_calls == _CAS_RETRIES
