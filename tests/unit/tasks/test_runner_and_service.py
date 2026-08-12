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


class _StolenExecutor(InProcessExecutor):
    """模拟"读到 PENDING 之后、真正入队之前，worker 抢先把任务捞走了"。

    这个缝在跨进程部署下是真实存在的：两次读之间隔着一次网络往返。
    进程内也存在，只是窄得多 —— 所以只能靠注入来确定性地复现。
    """

    async def submit(self, task_id: str, *, delay: float = 0.0) -> None:
        await self._store.transition(task_id, TaskStatus.RUNNING)  # 别人抢走了
        await super().submit(task_id, delay=delay)  # 于是这里必然拒绝


async def test_submit_tolerates_being_beaten_to_the_queue(
    store: TaskStore,
) -> None:
    """竞态下的重复提交不该变成 500 —— 任务已经排上了，目的达成。"""
    executor = _StolenExecutor(store, retry_backoff=0.01)
    svc = TaskService(store, executor)
    try:
        task = await svc.submit("rs-demo", {})
    finally:
        await executor.shutdown(timeout=5.0)

    assert task.status is TaskStatus.RUNNING


async def test_submit_still_raises_when_the_task_really_is_pending(
    store: TaskStore,
) -> None:
    """反面：任务仍在 PENDING 却入队失败，那是真错误，绝不能咽掉。

    没有这条，上面那个 try/except 就退化成"吞掉所有 ValueError"，
    执行器彻底坏掉时也会一声不吭。
    """

    class _BrokenExecutor(InProcessExecutor):
        async def submit(self, task_id: str, *, delay: float = 0.0) -> None:
            raise ValueError("执行器坏了")

    executor = _BrokenExecutor(store, retry_backoff=0.01)
    svc = TaskService(store, executor)

    with pytest.raises(ValueError, match="执行器坏了"):
        await svc.submit("rs-demo", {})


async def test_shutdown_error_is_never_swallowed(store: TaskStore) -> None:
    """执行器已关停时抛的是 `RuntimeError`，**不在 `_enqueue` 的捕获范围内**。

    这是对 PR 评审 #10 的回答：被咽掉的只有"别人已经接手了"这一种
    （`ValueError` + 任务已离开 PENDING）。关停、连接断开等等抛的都不是
    `ValueError`，照常上抛。
    """
    executor = InProcessExecutor(store, retry_backoff=0.01)
    await executor.shutdown(timeout=5.0)
    svc = TaskService(store, executor)

    with pytest.raises(RuntimeError, match="已关停"):
        await svc.submit("rs-demo", {})


async def test_swallowed_race_is_logged(store: TaskStore) -> None:
    """咽掉可以，但**不能静默** —— 否则真出了没预料到的情况也查不出来。"""
    from comet_rag.core.logging import logger  # noqa: PLC0415

    records: list[str] = []
    sink = logger.add(lambda m: records.append(m.record["message"]), level="INFO")
    executor = _StolenExecutor(store, retry_backoff=0.01)
    try:
        await TaskService(store, executor).submit("rs-demo", {})
    finally:
        logger.remove(sink)
        await executor.shutdown(timeout=5.0)

    assert any("已被接手" in r for r in records), f"咽掉了却没留痕：{records}"


async def test_unknown_state_is_never_swallowed(store: TaskStore) -> None:
    """抑制条件是**显式白名单**，不是"只要不是 PENDING"（PR 评审 #10）。

    这里伪造一个"submit 被拒、但任务仍停在一个不该被抑制的状态"的情形：
    白名单写法会上抛，反向条件写法（`!= PENDING`）会默默咽掉。

    真实价值在将来：状态机日后加一个新状态时，它会掉进 `raise` 分支被人看见，
    而不是被一句"反正不是 PENDING"顺手纳入抑制范围。
    """
    from comet_rag.tasks import service as service_module  # noqa: PLC0415

    class _RejectingExecutor(InProcessExecutor):
        async def submit(self, task_id: str, *, delay: float = 0.0) -> None:
            raise ValueError("只有 PENDING 可提交，当前 某个未来的新状态")

    executor = _RejectingExecutor(store, retry_backoff=0.01)
    svc = TaskService(store, executor)
    # 把白名单临时清空 = 模拟"当前状态不在已知的良性集合里"
    original = service_module._ALREADY_HANDLED
    service_module._ALREADY_HANDLED = frozenset()
    try:
        with pytest.raises(ValueError, match="只有 PENDING 可提交"):
            await svc.submit("rs-demo", {})
    finally:
        service_module._ALREADY_HANDLED = original
        await executor.shutdown(timeout=5.0)


async def _minutes_ago(store: TaskStore, task_id: str, minutes: int):
    from comet_rag.tasks import Time

    return Time.now() - timedelta(minutes=minutes)
