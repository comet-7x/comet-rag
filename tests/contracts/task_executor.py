"""`TaskExecutor` 契约：任何实现都必须通过的一套测试。

用法——实现方提供 `store` 与 `executor` 两个 fixture：

    class TestInProcessExecutor(TaskExecutorContract):
        @pytest.fixture
        async def executor(self, store):
            ex = InProcessExecutor(store, retry_backoff=0.01)
            yield ex
            await ex.shutdown()

**本契约只通过 TaskStore 观察结果**，绝不 gather 执行器的内部协程。
`InProcessExecutor` 有 `_bg` 可等，而 `ArqExecutor` 的任务跑在别的进程里，
压根没有本地协程 —— 只有把"跑完了"定义成"库里到终态了"，
两者才可能跑同一套测试。

runner 注册表是进程级全局的，故本模块的 runner 在导入时注册一次，
kind 一律带 `c-` 前缀，避免与其他测试模块的 runner 撞名。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from comet_rag.tasks import (
    Done,
    RetriableError,
    StagePipeline,
    TaskContext,
    TaskExecutor,
    TaskStatus,
    TaskStore,
    register,
    sleep_with_checkpoint,
)

from .support import wait_for_terminal, wait_until

# 每个用例的可变状态；由契约基类的 autouse fixture 清空
CSTATE: dict[str, Any] = {}


# ── 契约用 runner ──────────────────────────────────────────────────────────


@register("c-ok")
async def _ok(ctx: TaskContext) -> Done:
    CSTATE["ran"] = CSTATE.get("ran", 0) + 1
    return Done(result={"ok": True})


@register("c-slow")
async def _slow(ctx: TaskContext) -> Done:
    await ctx.enter_stage("working")
    await sleep_with_checkpoint(ctx, 5.0, step=0.02)
    return Done(result="不该走到这里")


@register("c-flaky")
async def _flaky(ctx: TaskContext) -> Done:
    CSTATE["ran"] = CSTATE.get("ran", 0) + 1
    if CSTATE["ran"] < 3:
        raise RetriableError(f"上游 503（第 {CSTATE['ran']} 次）", code="c_503")
    return Done(result={"tries": CSTATE["ran"]})


@register("c-doomed")
async def _doomed(ctx: TaskContext) -> Done:
    CSTATE["ran"] = CSTATE.get("ran", 0) + 1
    raise RetriableError("永远失败", code="c_doomed")


@register("c-boom")
async def _boom(ctx: TaskContext) -> Done:
    raise ValueError("确定性错误")


@register("c-concurrent")
async def _concurrent(ctx: TaskContext) -> Done:
    """记录并发峰值，用于验证闸门真的在限流（spec S4-2）。"""
    CSTATE["live"] = CSTATE.get("live", 0) + 1
    CSTATE["peak"] = max(CSTATE.get("peak", 0), CSTATE["live"])
    try:
        await sleep_with_checkpoint(ctx, 0.08, step=0.01)
    finally:
        CSTATE["live"] -= 1
    return Done(result="ok")


c_flow = StagePipeline()


@c_flow.stage("first")
async def _first(ctx: TaskContext) -> None:
    CSTATE.setdefault("visited", []).append("first")


@c_flow.stage("second")
async def _second(ctx: TaskContext) -> Done | None:
    CSTATE.setdefault("visited", []).append("second")
    CSTATE["ran"] = CSTATE.get("ran", 0) + 1
    if CSTATE["ran"] == 1:
        raise RetriableError("第一次在 second 失败", code="c_503")
    return Done(result="ok")


register("c-resumable")(c_flow)


# ── 契约 ───────────────────────────────────────────────────────────────────


class TaskExecutorContract:
    """子类必须覆写 `executor` fixture（并复用或覆写 `store`）。"""

    #: 实现方可覆写：跨进程实现需要更长的等待窗口
    timeout: float = 5.0

    @pytest.fixture(autouse=True)
    def _reset_contract_state(self):
        CSTATE.clear()
        yield
        CSTATE.clear()

    @pytest.fixture
    async def executor(self, store: TaskStore) -> TaskExecutor:  # pragma: no cover
        raise NotImplementedError("实现方必须提供 executor fixture")

    # ── 正常路径 ───────────────────────────────────────────────────────────

    async def test_submitted_task_runs_to_completion(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        task = await store.create("c-ok")
        await executor.submit(task.task_id)

        done = await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.result == {"ok": True}
        assert done.attempts == 1

    async def test_duplicate_submit_does_not_run_twice(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """网络重试、用户狂点提交都会造成重复投递，执行器必须幂等。"""
        task = await store.create("c-ok")
        await executor.submit(task.task_id)
        await executor.submit(task.task_id)

        await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        await asyncio.sleep(0.05)
        assert CSTATE.get("ran") == 1

    async def test_submit_non_pending_is_rejected(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        task = await store.create("c-ok")
        await store.transition(task.task_id, TaskStatus.RUNNING)

        with pytest.raises(ValueError):
            await executor.submit(task.task_id)

    async def test_unregistered_kind_fails_instead_of_crashing(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """未注册的 kind 是配置错误，必须落成任务失败，不能把 worker 带崩。"""
        task = await store.create("根本没注册过这个 kind")
        await executor.submit(task.task_id)

        done = await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        assert done.status is TaskStatus.FAILED
        assert done.error is not None

    # ── 失败与重试 ─────────────────────────────────────────────────────────

    async def test_retriable_failure_is_retried_until_success(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        task = await store.create("c-flaky", max_attempts=3)
        await executor.submit(task.task_id)

        done = await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.attempts == 3

    async def test_retries_stop_at_max_attempts(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        task = await store.create("c-doomed", max_attempts=2)
        await executor.submit(task.task_id)

        done = await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        assert done.status is TaskStatus.FAILED
        assert done.attempts == 2
        assert done.error is not None
        assert done.error.code == "c_doomed"

    async def test_non_retriable_error_does_not_consume_retries(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """确定性错误重试一万次也还是错的，不该浪费重试预算。"""
        task = await store.create("c-boom", max_attempts=5)
        await executor.submit(task.task_id)

        done = await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        assert done.status is TaskStatus.FAILED
        assert done.attempts == 1
        assert done.error is not None
        assert done.error.retriable is False

    async def test_retry_resumes_from_failed_stage(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """spec A10-修正：重试只重跑失败的阶段，不是整条重来。"""
        task = await store.create("c-resumable", max_attempts=3)
        await executor.submit(task.task_id)

        done = await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        assert done.status is TaskStatus.SUCCEEDED
        assert CSTATE["visited"] == ["first", "second", "second"]
        assert done.resume_stage is None, "成功后必须清空续跑锚点"

    # ── 取消 ───────────────────────────────────────────────────────────────

    async def test_cancel_running_task_reaches_cancelled(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """取消**必然**是协作式的：跨进程时 asyncio.Task.cancel 完全无效，
        只能靠 runner 走到 ctx.checkpoint() 时自己退出。"""
        task = await store.create("c-slow")
        await executor.submit(task.task_id)
        await wait_until(
            lambda: _is(store, task.task_id, TaskStatus.RUNNING),
            timeout=self.timeout,
            message="任务未进入 RUNNING",
        )

        assert await executor.request_cancel(task.task_id) is True

        done = await wait_for_terminal(store, task.task_id, timeout=self.timeout)
        assert done.status is TaskStatus.CANCELLED
        assert done.finished_at is not None

    async def test_cancel_pending_task_reaches_cancelled(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        task = await store.create("c-slow")

        assert await executor.request_cancel(task.task_id) is True
        assert (await store.require(task.task_id)).status is TaskStatus.CANCELLED

    async def test_cancel_unknown_returns_false(self, executor: TaskExecutor) -> None:
        assert await executor.request_cancel("不存在") is False

    async def test_cancel_terminal_returns_false(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        task = await store.create("c-ok")
        await executor.submit(task.task_id)
        await wait_for_terminal(store, task.task_id, timeout=self.timeout)

        assert await executor.request_cancel(task.task_id) is False

    # ── 并发闸门 ───────────────────────────────────────────────────────────

    async def test_concurrency_limit_is_enforced(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """spec S4-2：并发上限必须被真正执行，否则模型服务会被打爆。

        实现方通过 `max_concurrency` 类属性声明自己配了多少。
        """
        limit = self.max_concurrency
        ids = []
        for _ in range(limit * 3):
            task = await store.create("c-concurrent")
            ids.append(task.task_id)
            await executor.submit(task.task_id)

        for task_id in ids:
            await wait_for_terminal(store, task_id, timeout=self.timeout * 3)

        assert CSTATE["peak"] <= limit, f"并发峰值 {CSTATE['peak']} 超过上限 {limit}"
        assert CSTATE["peak"] > 1, "并发度恒为 1 说明根本没并行，闸门测了个寂寞"

    #: 实现方声明自己配置的并发上限
    max_concurrency: int = 4

    # ── 关停 ───────────────────────────────────────────────────────────────

    async def test_shutdown_leaves_no_task_stuck_running(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """关停后不得留下 RUNNING 僵尸 —— 那种任务没人会再推进它。"""
        task = await store.create("c-slow")
        await executor.submit(task.task_id)
        await wait_until(
            lambda: _is(store, task.task_id, TaskStatus.RUNNING),
            timeout=self.timeout,
            message="任务未进入 RUNNING",
        )

        await executor.shutdown(timeout=self.timeout)

        final = await store.require(task.task_id)
        assert final.status.is_terminal or final.status is TaskStatus.PENDING, (
            f"关停后任务停在 {final.status.value}，既没落终态也没退回排队"
        )

    async def test_submit_after_shutdown_is_rejected(
        self, store: TaskStore, executor: TaskExecutor
    ) -> None:
        """关停后必须拒绝新任务，否则它会被收下却永远没人执行。"""
        await executor.shutdown(timeout=self.timeout)
        task = await store.create("c-ok")

        with pytest.raises(RuntimeError):
            await executor.submit(task.task_id)


async def _is(store: TaskStore, task_id: str, status: TaskStatus) -> bool:
    task = await store.get(task_id)
    return task is not None and task.status is status
