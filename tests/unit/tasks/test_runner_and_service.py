"""Runner 注册表、TaskContext 与 TaskService 的行为。

契约测试覆盖的是 store/executor 的**跨实现**保证，这里补的是这两者之外
仅有一种实现、但同样会咬人的部分：重复注册守卫、协作取消检查点、
进度钳制、以及 service 门面上那些"必须成对发生"的动作。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from comet_rag.tasks import (
    Done,
    InMemoryTaskStore,
    InProcessExecutor,
    StagePipeline,
    TaskCancelled,
    TaskContext,
    TaskService,
    TaskStatus,
    TaskStore,
    get_runner,
    register,
    registered_kinds,
    sleep_with_checkpoint,
)
from comet_rag.tasks.runner import UnknownKind
from tests.contracts.support import wait_for_terminal


@pytest.fixture
def store() -> InMemoryTaskStore:
    return InMemoryTaskStore()


@pytest.fixture
async def executor(store: InMemoryTaskStore) -> AsyncIterator[InProcessExecutor]:
    ex = InProcessExecutor(store, max_concurrency=4, retry_backoff=0.01)
    yield ex
    await ex.shutdown(timeout=5.0)


@pytest.fixture
def svc(store: InMemoryTaskStore, executor: InProcessExecutor) -> TaskService:
    return TaskService(store, executor)


@pytest.fixture
def ctx(store: InMemoryTaskStore) -> TaskContext:
    return TaskContext(store, "占位", worker_id="w1")


# ── 注册表 ─────────────────────────────────────────────────────────────────


@register("rs-demo")
async def _demo(ctx: TaskContext) -> Done:
    return Done(result="ok")


def test_duplicate_registration_is_rejected() -> None:
    """静默覆盖会让"明明注册了却跑的是别人的 runner"极难排查。"""
    with pytest.raises(ValueError, match="重复注册"):
        register("rs-demo")(_demo)


def test_get_runner_returns_registered() -> None:
    assert get_runner("rs-demo") is _demo


def test_get_unknown_kind_raises() -> None:
    with pytest.raises(UnknownKind):
        get_runner("从未注册过")


def test_registered_kinds_is_sorted() -> None:
    kinds = registered_kinds()
    assert kinds == sorted(kinds)
    assert "rs-demo" in kinds


# ── TaskContext ────────────────────────────────────────────────────────────


async def test_snapshot_returns_current_state(store: TaskStore) -> None:
    task = await store.create("rs-demo")
    ctx = TaskContext(store, task.task_id)

    assert (await ctx.snapshot()).task_id == task.task_id


async def test_checkpoint_raises_when_cancelling(store: TaskStore) -> None:
    """协作式取消的全部机制就在这一行：runner 自己检查、自己退出。"""
    task = await store.create("rs-demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    await store.transition(task.task_id, TaskStatus.CANCELLING)
    ctx = TaskContext(store, task.task_id)

    with pytest.raises(TaskCancelled):
        await ctx.checkpoint()


async def test_checkpoint_is_a_noop_while_running(store: TaskStore) -> None:
    task = await store.create("rs-demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    ctx = TaskContext(store, task.task_id)

    await ctx.checkpoint()  # 不抛即通过


async def test_checkpoint_heartbeats_after_interval(store: TaskStore) -> None:
    task = await store.create("rs-demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    await store.update(
        task.task_id, heartbeat_at=await _minutes_ago(store, task.task_id, 1)
    )
    ctx = TaskContext(store, task.task_id, heartbeat_interval=timedelta(0))
    before = (await store.require(task.task_id)).heartbeat_at

    await ctx.checkpoint()

    after = (await store.require(task.task_id)).heartbeat_at
    assert before is not None and after is not None and after > before


async def test_put_merges_into_context(store: TaskStore) -> None:
    """续跑靠 context，覆盖式写入会把上一阶段的产出抹掉。"""
    task = await store.create("rs-demo")
    ctx = TaskContext(store, task.task_id)

    await ctx.put(a=1)
    await ctx.put(b=2)

    assert (await store.require(task.task_id)).context == {"a": 1, "b": 2}


@pytest.mark.parametrize(
    ("given", "expected"), [(-0.5, 0.0), (0.0, 0.0), (0.4, 0.4), (1.0, 1.0), (7.0, 1.0)]
)
async def test_report_clamps_progress(
    store: TaskStore, given: float, expected: float
) -> None:
    """进度直接进前端进度条，越界值会画出诡异的 UI。"""
    task = await store.create("rs-demo")
    ctx = TaskContext(store, task.task_id)

    await ctx.report(progress=given)

    assert (await store.require(task.task_id)).progress == expected


async def test_report_without_fields_is_a_noop(store: TaskStore) -> None:
    task = await store.create("rs-demo")
    ctx = TaskContext(store, task.task_id)
    before = (await store.require(task.task_id)).version

    await ctx.report()

    assert (await store.require(task.task_id)).version == before


async def test_sleep_with_checkpoint_aborts_on_cancel(store: TaskStore) -> None:
    """长等待必须切成小段，否则取消要等到 sleep 结束才生效。"""
    task = await store.create("rs-demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    await store.transition(task.task_id, TaskStatus.CANCELLING)
    ctx = TaskContext(store, task.task_id)

    with pytest.raises(TaskCancelled):
        await sleep_with_checkpoint(ctx, 10.0, step=0.01)


# ── StagePipeline ──────────────────────────────────────────────────────────

early_stop = StagePipeline()
_VISITED: list[str] = []


@early_stop.stage("one")
async def _one(ctx: TaskContext) -> None:
    _VISITED.append("one")


@early_stop.stage("two")
async def _two(ctx: TaskContext) -> Done:
    _VISITED.append("two")
    return Done(result="提前收工")


@early_stop.stage("three")
async def _three(ctx: TaskContext) -> None:  # pragma: no cover - 不该被执行
    _VISITED.append("three")


register("rs-early-stop")(early_stop)


async def test_stage_returning_done_short_circuits(
    svc: TaskService, store: TaskStore
) -> None:
    _VISITED.clear()
    task = await svc.submit("rs-early-stop", {})

    done = await wait_for_terminal(store, task.task_id)

    assert done.status is TaskStatus.SUCCEEDED
    assert done.result == "提前收工"
    assert _VISITED == ["one", "two"], "返回 Done 之后的阶段不该再跑"


# ── TaskService ────────────────────────────────────────────────────────────


async def test_get_and_list_and_events(svc: TaskService, store: TaskStore) -> None:
    task = await svc.submit("rs-demo", {"x": 1}, owner_id="u1")
    await wait_for_terminal(store, task.task_id)

    assert (await svc.get(task.task_id)) is not None
    assert await svc.get("不存在") is None
    assert len(await svc.list(owner_id="u1")) == 1
    assert len(await svc.list(kind="rs-demo")) == 1
    assert len(await svc.events(task.task_id)) > 0


async def test_retry_rejects_non_failed_task(
    svc: TaskService, store: TaskStore
) -> None:
    """只有 FAILED 可重试。放行别的状态会造成一份任务两个执行者。"""
    task = await svc.submit("rs-demo", {})
    await wait_for_terminal(store, task.task_id)

    with pytest.raises(ValueError, match="只有 FAILED 可重试"):
        await svc.retry(task.task_id)


async def test_delete_removes_idle_task(svc: TaskService, store: TaskStore) -> None:
    task = await svc.submit("rs-demo", {})
    await wait_for_terminal(store, task.task_id)

    assert await svc.delete(task.task_id) is True
    assert await svc.get(task.task_id) is None


async def test_force_delete_cancels_first(svc: TaskService, store: TaskStore) -> None:
    """force 删除必须先受理取消，否则协程还在跑、任务记录却没了。"""
    task = await store.create("rs-demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)

    assert await svc.delete(task.task_id, force=True) is True
    assert await svc.get(task.task_id) is None


async def _minutes_ago(store: TaskStore, task_id: str, minutes: int):
    from comet_rag.tasks import Time

    return Time.now() - timedelta(minutes=minutes)
