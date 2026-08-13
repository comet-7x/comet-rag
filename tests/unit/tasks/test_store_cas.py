"""`TaskStore._cas` 的重读重试策略。

这段行为是 T22 的集成测试逼出来的：跨进程部署下，runner 一边在
`enter_stage`，另一边有人请求取消，两次写入撞在同一个版本号上，
取消直接抛 `VersionConflict` 失败了 —— 任务永远停不下来。

集成测试能复现（PostgreSQL + 真 Redis 下窗口宽到必撞），但它依赖时序。
这里用**注入一次冲突**的方式把同一件事测成确定的：不需要中间件，
也不会因为机器快慢而时灵时不灵。
"""

from __future__ import annotations

import asyncio

import pytest

from comet_rag.tasks import (
    Done,
    InMemoryTaskStore,
    InProcessExecutor,
    Task,
    TaskContext,
    TaskStatus,
    VersionConflict,
    register,
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


# ── 认领必须原子（PR 评审 #1）────────────────────────────────────────────────


class _RacyClaimStore(InMemoryTaskStore):
    """让两个执行器都读到同一份 PENDING 快照，再放它们去抢。

    不加这道屏障的话，asyncio 的调度往往让第一个 `execute()` 一口气跑完认领，
    危险窗口根本不出现 —— 测试会绿，但它**什么也没验到**。
    """

    def __init__(self, barrier: asyncio.Barrier) -> None:
        super().__init__()
        self.barrier = barrier
        self.armed = False

    async def _load(self, task_id: str):
        task = await super()._load(task_id)
        if self.armed and task is not None and task.status is TaskStatus.PENDING:
            await self.barrier.wait()  # 都读完了，再一起放行
        return task


async def test_two_executors_cannot_both_claim_the_same_task() -> None:
    """**同一个任务只能被认领一次**，否则 runner 的副作用会跑两遍。

    陷阱比"少个锁"深一层：`RUNNING → RUNNING` 是合法的自迁移，而 `_cas`
    对没传版本号的冲突会重读重试 —— 于是输的那个不但没被拦下，还会把赢家的
    `worker_id` 覆盖掉，围栏（`_still_mine`）反过来把**真正在跑的那个**挡在门外。

    ⚠️ 这条用例的关键在于**赢家必须还停在 RUNNING 上**。第一版写成瞬间返回的
    runner，结果赢家早已落到 SUCCEEDED，输家重试时被状态机自然拦下
    （SUCCEEDED → RUNNING 非法），用例于是**在有 bug 的代码上照样绿**。
    加了 `gate` 把赢家按在 RUNNING 上，才真正复现出 `runs == ["B", "A"]`。
    """
    runs: list[str] = []
    gate = asyncio.Event()

    @register("cas-claim-race", replace=True)
    async def _runner(ctx: TaskContext) -> Done:
        runs.append(ctx.worker_id or "?")
        await gate.wait()  # 赢家停在 RUNNING 上，把危险窗口撑开
        return Done(result="ok")

    barrier = asyncio.Barrier(2)
    store = _RacyClaimStore(barrier)
    task = await store.create("cas-claim-race")

    a = InProcessExecutor(store, worker_id="worker-A")
    b = InProcessExecutor(store, worker_id="worker-B")
    store.armed = True
    running = asyncio.ensure_future(
        asyncio.gather(
            a.execute(task.task_id), b.execute(task.task_id), return_exceptions=True
        )
    )
    try:
        await asyncio.sleep(0.2)  # 两边都走到 runner / 重试之后再看
        claimed_while_running = list(runs)
        gate.set()
        await running
    finally:
        store.armed = False
        gate.set()
        await a.shutdown(timeout=5.0)
        await b.shutdown(timeout=5.0)

    assert len(claimed_while_running) == 1, (
        f"任务被 {claimed_while_running} 同时执行 —— 认领不是原子的"
    )
    final = await store.require(task.task_id)
    assert final.status is TaskStatus.SUCCEEDED
    assert final.attempts == 1, f"attempts={final.attempts}，有人重复认领过"
    assert final.worker_id == claimed_while_running[0], (
        "任务上记的 worker 不是真正在跑它的那个 —— 输家把赢家的所有权覆盖了"
    )
