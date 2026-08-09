"""任务框架行为基线 —— 由 poc/task_demo/demo.py 的 5 个场景转写而来。

原 demo 用 print 展示行为，人看着"像是对的"就算过。这里改成断言，
使其成为 T5 迁移（poc/task_demo/task/ → comet_rag/tasks/）的安全网：
搬运过程中任何行为漂移都会当场变红。

原场景 1 建立在确认门之上，而 spec A10 决定移除确认门，
故改写为"多阶段推进 + 断点续跑"—— 只覆盖将被保留的机制。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from comet_rag.tasks import (
    InProcessExecutor,
    InvalidTransition,
    Task,
    TaskService,
    TaskStatus,
    TaskStore,
    Time,
    VersionConflict,
)

# ── 场景 1：多阶段推进 ──────────────────────────────────────────────────────


async def test_multi_stage_pipeline_runs_all_stages_in_order(
    svc: TaskService, executor: InProcessExecutor, state: dict
) -> None:
    task = await svc.submit("multi", {"topic": "光合作用"}, owner_id="u1")
    await executor.wait_all()

    task = await svc.store.require(task.task_id)
    assert task.status is TaskStatus.SUCCEEDED
    assert state["visited"] == ["extract", "transform", "load"]
    assert task.result == {"count": 2}
    assert task.result_uri == "s3://out"
    assert task.progress == 1.0


async def test_stage_history_records_every_stage(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    """阶段留痕是"卡在哪一步"的唯一线索，前端进度条也依赖它。"""
    task = await svc.submit("multi", {})
    await executor.wait_all()

    task = await svc.store.require(task.task_id)
    assert [r.stage for r in task.stage_history] == ["extract", "transform", "load"]
    assert all(r.finished_at is not None for r in task.stage_history)
    assert all(r.status == "succeeded" for r in task.stage_history)


async def test_event_stream_is_sequential(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    task = await svc.submit("multi", {})
    await executor.wait_all()

    events = await svc.events(task.task_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].type == "created"
    assert {e.type for e in events} >= {"created", "transition", "stage"}


async def test_idempotency_key_returns_existing_task(
    svc: TaskService, executor: InProcessExecutor, state: dict
) -> None:
    """防前端重复提交。同 key 再提交必须命中原任务，而不是跑两遍。"""
    first = await svc.submit("multi", {"topic": "x"}, idempotency_key="k-1")
    again = await svc.submit("multi", {"topic": "x"}, idempotency_key="k-1")
    await executor.wait_all()

    assert again.task_id == first.task_id
    assert state["visited"] == ["extract", "transform", "load"]  # 只跑了一遍


async def test_retry_resumes_from_failed_stage(
    svc: TaskService, executor: InProcessExecutor, state: dict
) -> None:
    """重试只重跑失败的那个阶段（spec A10-修正）。

    对 RAG 入库链路而言这不是优化而是必要 —— embedding 阶段因模型服务 503
    失败时，不该把已经花了几秒 CPU 的 docx 解析和分块整个重做一遍。
    """
    task = await svc.submit("resumable", {}, max_attempts=3)
    await _settle(executor)

    assert (await svc.store.require(task.task_id)).status is TaskStatus.SUCCEEDED
    assert state["visited"] == ["s1", "s2", "s3", "s3"], "s1/s2 不该被重跑"


async def test_stage_history_marks_the_failed_attempt(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    """失败那次的阶段留痕必须记成 failed。

    续跑时 enter_stage 会关掉"上一条还开着口的记录"，若不在退回 PENDING 时
    先把它收成 failed，这条失败记录会被关成 succeeded —— 阶段历史就骗人了。
    """
    task = await svc.submit("resumable", {}, max_attempts=3)
    await _settle(executor)

    history = [
        (r.stage, r.status)
        for r in (await svc.store.require(task.task_id)).stage_history
    ]
    assert history == [
        ("s1", "succeeded"),
        ("s2", "succeeded"),
        ("s3", "failed"),
        ("s3", "succeeded"),
    ]


async def test_success_clears_the_resume_anchor(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    """成功后必须清空 resume_stage，否则日后显式 retry 会从半途开始。"""
    task = await svc.submit("resumable", {}, max_attempts=3)
    await _settle(executor)

    assert (await svc.store.require(task.task_id)).resume_stage is None


async def test_explicit_retry_from_scratch_restarts_whole_pipeline(
    svc: TaskService, executor: InProcessExecutor, state: dict
) -> None:
    """怀疑是前置阶段产出有问题时，from_scratch=True 强制整条重来。"""
    task = await svc.submit("resumable", {}, max_attempts=1)
    await _settle(executor)
    assert (await svc.store.require(task.task_id)).status is TaskStatus.FAILED
    assert state["visited"] == ["s1", "s2", "s3"]

    await svc.retry(task.task_id, from_scratch=True)
    await _settle(executor)

    assert (await svc.store.require(task.task_id)).status is TaskStatus.SUCCEEDED
    assert state["visited"] == ["s1", "s2", "s3", "s1", "s2", "s3"]


async def test_explicit_retry_defaults_to_resuming(
    svc: TaskService, executor: InProcessExecutor, state: dict
) -> None:
    """判死后的显式 retry 默认也续跑 —— _mark_failed 两个分支都写了锚点。"""
    task = await svc.submit("resumable", {}, max_attempts=1)
    await _settle(executor)
    assert (await svc.store.require(task.task_id)).resume_stage == "s3"

    await svc.retry(task.task_id)
    await _settle(executor)

    assert (await svc.store.require(task.task_id)).status is TaskStatus.SUCCEEDED
    assert state["visited"] == ["s1", "s2", "s3", "s3"]


# ── 场景 2：协作式取消 ──────────────────────────────────────────────────────


async def test_cancel_is_accepted_then_eventually_terminal(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    """cancel() 返回 True 只代表**已受理**，不代表已停止。

    真正落 CANCELLED 要等 runner 走到下一个 ctx.checkpoint()。
    调用方要确认"真停了"必须查 status.is_terminal。
    """
    task = await svc.submit("slow", {})
    await asyncio.sleep(0.05)

    accepted = await svc.cancel(task.task_id)
    assert accepted is True

    await executor.wait_all()
    task = await svc.store.require(task.task_id)
    assert task.status is TaskStatus.CANCELLED
    assert task.finished_at is not None
    assert task.heartbeat_at is None


async def test_cancel_pending_task_goes_straight_to_terminal(
    svc: TaskService, store: TaskStore
) -> None:
    """PENDING 任务没有在跑的 runner，可以直接落终态，不必经 CANCELLING。"""
    task = await store.create("slow")
    accepted = await svc.cancel(task.task_id)

    assert accepted is True
    assert (await store.require(task.task_id)).status is TaskStatus.CANCELLED


async def test_cancel_unknown_task_returns_false(svc: TaskService) -> None:
    assert await svc.cancel("不存在的任务") is False


# ── 场景 3：可重试失败自动退避重排队 ────────────────────────────────────────


async def test_retriable_failure_is_retried_until_success(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    task = await svc.submit("flaky", {}, max_attempts=3)
    await _settle(executor)

    task = await svc.store.require(task.task_id)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.attempts == 3
    assert task.result == {"ok": True, "tries": 3}


async def test_retries_stop_at_max_attempts(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    task = await svc.submit("always_fails", {}, max_attempts=2)
    await _settle(executor)

    task = await svc.store.require(task.task_id)
    assert task.status is TaskStatus.FAILED
    assert task.attempts == 2
    assert task.error is not None
    assert task.error.code == "doomed"


async def test_non_retriable_error_fails_immediately(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    """确定性错误（解析失败等）不该消耗重试次数 —— 重试一万次也还是坏文件。"""
    task = await svc.submit("boom", {}, max_attempts=5)
    await _settle(executor)

    task = await svc.store.require(task.task_id)
    assert task.status is TaskStatus.FAILED
    assert task.attempts == 1
    assert task.error is not None
    assert task.error.code == "ValueError"
    assert task.error.retriable is False


async def test_failed_task_can_be_reopened_by_explicit_retry(
    svc: TaskService, executor: InProcessExecutor, state: dict
) -> None:
    task = await svc.submit("always_fails", {}, max_attempts=1)
    await _settle(executor)
    assert (await svc.store.require(task.task_id)).status is TaskStatus.FAILED

    state["attempts"] = 0  # 让它这次能成功？不能 —— always_fails 永远失败
    reopened = await svc.retry(task.task_id)
    assert reopened.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
    assert reopened.error is None
    await _settle(executor)


# ── 场景 4：乐观锁、状态机守卫与序列化往返 ──────────────────────────────────


async def test_stale_version_write_raises_conflict(store: TaskStore) -> None:
    """并发写靠 CAS 失败重试，而不是靠祈祷。"""
    task = await store.create("multi")
    stale = task.version

    await store.update(task.task_id, message="别人先写了")

    with pytest.raises(VersionConflict):
        await store.update(task.task_id, message="我拿旧版本写", expected_version=stale)


async def test_concurrent_writes_exactly_one_wins(store: TaskStore) -> None:
    task = await store.create("multi")
    version = task.version

    async def write(n: int) -> None:
        await store.update(task.task_id, message=f"w{n}", expected_version=version)

    results = await asyncio.gather(
        *(write(i) for i in range(10)), return_exceptions=True
    )

    ok = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, VersionConflict)]
    assert len(ok) == 1, "同一版本上应恰好一个写入成功"
    assert len(conflicts) == 9


async def test_terminal_state_cannot_be_left(store: TaskStore) -> None:
    """SUCCEEDED / CANCELLED 是死路。

    注意必须先 PENDING → RUNNING 才能到 SUCCEEDED：demo.py 原本直接
    transition(SUCCEEDED)，抛的其实是 PENDING → SUCCEEDED 非法，
    而非它想演示的"终态不可迁出"—— print 式演示掩盖了这个区别。
    """
    task = await store.create("multi")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    await store.transition(task.task_id, TaskStatus.SUCCEEDED)

    with pytest.raises(InvalidTransition):
        await store.transition(task.task_id, TaskStatus.RUNNING)


async def test_pending_cannot_jump_straight_to_succeeded(store: TaskStore) -> None:
    """跳过 RUNNING 直接判成功是非法的 —— 否则"没跑过的任务成功了"。"""
    task = await store.create("multi")

    with pytest.raises(InvalidTransition):
        await store.transition(task.task_id, TaskStatus.SUCCEEDED)


async def test_failed_can_reopen_to_pending(store: TaskStore) -> None:
    """FAILED 是 is_terminal 的唯一例外：可被**显式** retry 重新打开。"""
    task = await store.create("multi")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    await store.transition(task.task_id, TaskStatus.FAILED)

    reopened = await store.transition(task.task_id, TaskStatus.PENDING)
    assert reopened.status is TaskStatus.PENDING
    assert reopened.finished_at is None, "重新打开后不该还留着完成时间"


async def test_update_refuses_to_change_status(store: TaskStore) -> None:
    """status 必须走 transition()，否则状态机守卫形同虚设。"""
    task = await store.create("multi")

    with pytest.raises(ValueError, match="status"):
        await store.update(task.task_id, status=TaskStatus.PENDING)


async def test_update_rejects_unknown_field(store: TaskStore) -> None:
    task = await store.create("multi")

    with pytest.raises(ValueError, match="没有这些字段"):
        await store.update(task.task_id, reslut="拼错了")


async def test_serialization_round_trip_is_lossless(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    """跨进程恢复的前提：Task 必须能无损地过一遍 JSON。"""
    task = await svc.submit("multi", {"topic": "并发"})
    await executor.wait_all()

    fresh = await svc.store.require(task.task_id)
    restored = Task.from_dict(fresh.to_dict())
    assert restored == fresh


async def test_public_view_hides_internal_fields(
    svc: TaskService, executor: InProcessExecutor
) -> None:
    """给前端的裁剪视图不得泄漏 traceback、worker_id 等内部信息。"""
    task = await svc.submit("boom", {}, max_attempts=1)
    await _settle(executor)

    view = (await svc.store.require(task.task_id)).public_view()
    for leaked in ("worker_id", "version", "idempotency_key", "context"):
        assert leaked not in view
    assert set(view["error"]) == {"code", "message"}, "错误详情不应带 traceback"


# ── 场景 5：租约过期回收僵尸任务 ────────────────────────────────────────────


async def test_heartbeat_does_not_bump_version(store: TaskStore) -> None:
    """心跳几秒一次，若也涨版本号会把所有 runner 手里的 expected_version 撞失效，
    乐观锁退化成"不停重试"。语义上心跳不属于任务状态变更。
    """
    task = await store.create("flaky")
    await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="w1")

    before = (await store.require(task.task_id)).version
    await store.heartbeat(task.task_id)
    after = (await store.require(task.task_id)).version

    assert before == after


async def test_sweep_stale_requeues_zombie_with_attempts_left(
    store: TaskStore,
) -> None:
    """跨进程部署必踩的坑：worker 崩了没人再写心跳，任务永远卡在 RUNNING。"""
    task = await store.create("flaky", max_attempts=2)
    await store.transition(
        task.task_id, TaskStatus.RUNNING, attempts=1, worker_id="dead-worker"
    )
    await store.update(task.task_id, heartbeat_at=Time.now() - timedelta(minutes=5))

    revived = await store.sweep_stale(lease=timedelta(seconds=30))

    task = await store.require(task.task_id)
    assert revived == [task.task_id]
    assert task.status is TaskStatus.PENDING
    assert task.error is not None
    assert task.error.code == "lease_expired"
    assert task.worker_id is None


async def test_sweep_stale_fails_zombie_without_attempts_left(
    store: TaskStore,
) -> None:
    task = await store.create("flaky", max_attempts=1)
    await store.transition(
        task.task_id, TaskStatus.RUNNING, attempts=1, worker_id="dead-worker"
    )
    await store.update(task.task_id, heartbeat_at=Time.now() - timedelta(minutes=5))

    await store.sweep_stale(lease=timedelta(seconds=30))

    assert (await store.require(task.task_id)).status is TaskStatus.FAILED


async def test_sweep_stale_leaves_live_tasks_alone(store: TaskStore) -> None:
    """租约未过期的任务不得被误回收 —— 否则一份任务会有两个执行者。"""
    task = await store.create("flaky", max_attempts=2)
    await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="alive")

    assert await store.sweep_stale(lease=timedelta(seconds=30)) == []
    assert (await store.require(task.task_id)).status is TaskStatus.RUNNING


# ── 辅助 ────────────────────────────────────────────────────────────────────


async def _settle(executor: InProcessExecutor, *, timeout: float = 5.0) -> None:
    """等到执行器彻底静止。

    不能只 wait_all()：可重试失败会派生一个"等退避再重投"的独立协程，
    它在下一轮才会把任务重新挂上 _bg。所以要反复等到确实没有活口。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await executor.wait_all()
        await asyncio.sleep(0.05)
        if not any(not t.done() for t in (*executor._bg.values(), *executor._detached)):
            return
    raise AssertionError("执行器在超时内未静止")
