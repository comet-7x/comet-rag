"""`TaskStore` 契约：任何实现都必须通过的一套测试。

用法——实现方只需提供 `store` fixture：

    class TestInMemoryTaskStore(TaskStoreContract):
        @pytest.fixture
        async def store(self):
            return InMemoryTaskStore()

`InMemoryTaskStore` 与将来的 `PostgresTaskStore` 跑**同一套**测试，
这是"换存储时行为不变"这句承诺唯一的兑现手段。口头约定做不到这一点：
CAS 语义、心跳不涨版本、租约回收这些规则散落在模板方法里，
换实现时最容易在不经意间破坏，而破坏后只在生产偶发。

契约只依赖 `TaskStore` 的公开 API，不碰任何实现细节。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from comet_rag.tasks import (
    InvalidTransition,
    Task,
    TaskBusy,
    TaskNotFound,
    TaskStatus,
    TaskStore,
    Time,
    VersionConflict,
)


class TaskStoreContract:
    """子类必须覆写 `store` fixture。类名不以 Test 开头，故本身不被收集。"""

    @pytest.fixture
    async def store(self) -> TaskStore:  # pragma: no cover - 由子类提供
        raise NotImplementedError("实现方必须提供 store fixture")

    # ── 创建与读取 ─────────────────────────────────────────────────────────

    async def test_create_then_get(self, store: TaskStore) -> None:
        task = await store.create("demo", request={"a": 1}, owner_id="u1")

        loaded = await store.require(task.task_id)
        assert loaded.task_id == task.task_id
        assert loaded.kind == "demo"
        assert loaded.request == {"a": 1}
        assert loaded.owner_id == "u1"
        assert loaded.status is TaskStatus.PENDING

    async def test_get_unknown_returns_none(self, store: TaskStore) -> None:
        assert await store.get("不存在") is None

    async def test_require_unknown_raises(self, store: TaskStore) -> None:
        with pytest.raises(TaskNotFound):
            await store.require("不存在")

    async def test_loaded_task_is_a_copy(self, store: TaskStore) -> None:
        """返回活对象会让调用方绕过 CAS 直接改库 —— 乐观锁就形同虚设了。"""
        task = await store.create("demo")

        detached = await store.require(task.task_id)
        detached.message = "偷偷改的"
        detached.context["x"] = 1

        fresh = await store.require(task.task_id)
        assert fresh.message == ""
        assert fresh.context == {}

    # ── 幂等 ───────────────────────────────────────────────────────────────

    async def test_idempotency_key_returns_existing(self, store: TaskStore) -> None:
        first = await store.create("demo", idempotency_key="k")
        again = await store.create("demo", idempotency_key="k")

        assert again.task_id == first.task_id
        assert len(await store.list_tasks(kind="demo")) == 1

    async def test_idempotency_key_is_scoped_by_kind(self, store: TaskStore) -> None:
        a = await store.create("demo", idempotency_key="k")
        b = await store.create("other", idempotency_key="k")

        assert a.task_id != b.task_id

    async def test_no_idempotency_key_always_creates(self, store: TaskStore) -> None:
        a = await store.create("demo")
        b = await store.create("demo")

        assert a.task_id != b.task_id

    # ── 查询 ───────────────────────────────────────────────────────────────

    async def test_list_filters_by_kind_and_owner(self, store: TaskStore) -> None:
        await store.create("a", owner_id="u1")
        await store.create("a", owner_id="u2")
        await store.create("b", owner_id="u1")

        assert len(await store.list_tasks(kind="a")) == 2
        assert len(await store.list_tasks(owner_id="u1")) == 2
        assert len(await store.list_tasks(kind="a", owner_id="u1")) == 1

    async def test_list_filters_by_status(self, store: TaskStore) -> None:
        running = await store.create("a")
        await store.create("a")
        await store.transition(running.task_id, TaskStatus.RUNNING)

        pending = await store.list_tasks(status=TaskStatus.PENDING)
        assert [t.task_id for t in pending] == [
            t.task_id for t in pending if t.status is TaskStatus.PENDING
        ]
        assert len(await store.list_tasks(status=TaskStatus.RUNNING)) == 1

    async def test_list_paginates(self, store: TaskStore) -> None:
        for _ in range(5):
            await store.create("a")

        assert len(await store.list_tasks(kind="a", limit=2)) == 2
        assert len(await store.list_tasks(kind="a", limit=2, offset=4)) == 1
        assert len(await store.list_tasks(kind="a", limit=10, offset=5)) == 0

    # ── 更新与乐观锁 ───────────────────────────────────────────────────────

    async def test_update_changes_field_and_bumps_version(
        self, store: TaskStore
    ) -> None:
        task = await store.create("demo")
        before = task.version

        updated = await store.update(task.task_id, message="进行中", progress=0.5)

        assert updated.message == "进行中"
        assert updated.progress == 0.5
        assert updated.version == before + 1

    async def test_update_rejects_immutable_fields(self, store: TaskStore) -> None:
        task = await store.create("demo")

        # 不含 task_id：它是 update() 的位置参数，作为字段传会撞成 TypeError，
        # 压根到不了 _check_fields。签名本身已经挡住了。
        for field, value in (
            ("kind", "别的"),
            ("version", 99),
            ("created_at", Time.now()),
            ("status", TaskStatus.RUNNING),
        ):
            with pytest.raises(ValueError):
                await store.update(task.task_id, **{field: value})

    async def test_update_rejects_unknown_field(self, store: TaskStore) -> None:
        """`slots=True` 之外的第二道防线：拼错字段名当场报错而非静默丢弃。"""
        task = await store.create("demo")

        with pytest.raises(ValueError, match="没有这些字段"):
            await store.update(task.task_id, reslut="拼错了")

    async def test_stale_expected_version_conflicts(self, store: TaskStore) -> None:
        task = await store.create("demo")
        stale = task.version
        await store.update(task.task_id, message="别人先写了")

        with pytest.raises(VersionConflict):
            await store.update(task.task_id, message="我", expected_version=stale)

    async def test_concurrent_cas_exactly_one_wins(self, store: TaskStore) -> None:
        task = await store.create("demo")
        version = task.version

        async def write(n: int) -> None:
            await store.update(task.task_id, message=f"w{n}", expected_version=version)

        results = await asyncio.gather(
            *(write(i) for i in range(10)), return_exceptions=True
        )

        winners = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, VersionConflict)]
        assert len(winners) == 1
        assert len(conflicts) == 9

    # ── 状态迁移 ───────────────────────────────────────────────────────────

    async def test_transition_enforces_state_machine(self, store: TaskStore) -> None:
        task = await store.create("demo")

        with pytest.raises(InvalidTransition):
            await store.transition(task.task_id, TaskStatus.SUCCEEDED)

    async def test_transition_to_running_sets_timestamps(
        self, store: TaskStore
    ) -> None:
        task = await store.create("demo")

        running = await store.transition(task.task_id, TaskStatus.RUNNING)

        assert running.started_at is not None
        assert running.heartbeat_at is not None
        assert running.finished_at is None

    async def test_transition_to_succeeded_finalizes(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)

        done = await store.transition(task.task_id, TaskStatus.SUCCEEDED)

        assert done.finished_at is not None
        assert done.heartbeat_at is None
        assert done.progress == 1.0

    async def test_requeue_clears_worker_and_finish_time(
        self, store: TaskStore
    ) -> None:
        task = await store.create("demo", max_attempts=2)
        await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="w1")

        requeued = await store.transition(task.task_id, TaskStatus.PENDING)

        assert requeued.worker_id is None
        assert requeued.finished_at is None
        assert requeued.heartbeat_at is None

    async def test_failed_can_be_reopened(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)
        await store.transition(task.task_id, TaskStatus.FAILED)

        reopened = await store.transition(task.task_id, TaskStatus.PENDING)
        assert reopened.status is TaskStatus.PENDING

    async def test_terminal_states_are_dead_ends(self, store: TaskStore) -> None:
        """SUCCEEDED / CANCELLED 不可再迁出（FAILED 是唯一例外，见上一个用例）。

        注意到达 CANCELLED 必须经 CANCELLING —— 取消是协作式的，
        RUNNING 不能直接跳 CANCELLED，因为那一刻 runner 其实还在跑。
        """
        # SUCCEEDED
        a = await store.create("demo")
        await store.transition(a.task_id, TaskStatus.RUNNING)
        await store.transition(a.task_id, TaskStatus.SUCCEEDED)
        with pytest.raises(InvalidTransition):
            await store.transition(a.task_id, TaskStatus.RUNNING)

        # CANCELLED
        b = await store.create("demo")
        await store.transition(b.task_id, TaskStatus.RUNNING)
        await store.transition(b.task_id, TaskStatus.CANCELLING)
        await store.transition(b.task_id, TaskStatus.CANCELLED)
        with pytest.raises(InvalidTransition):
            await store.transition(b.task_id, TaskStatus.RUNNING)

    # ── 阶段留痕 ───────────────────────────────────────────────────────────

    async def test_enter_stage_opens_and_closes_records(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.enter_stage(task.task_id, "one")
        await store.enter_stage(task.task_id, "two")

        history = (await store.require(task.task_id)).stage_history
        assert [r.stage for r in history] == ["one", "two"]
        assert history[0].finished_at is not None
        assert history[0].status == "succeeded"
        assert history[1].finished_at is None

    async def test_requeue_with_error_closes_stage_as_failed(
        self, store: TaskStore
    ) -> None:
        """失败那次的留痕必须记 failed，否则续跑时会被 enter_stage 关成 succeeded。"""
        task = await store.create("demo", max_attempts=2)
        await store.transition(task.task_id, TaskStatus.RUNNING)
        await store.enter_stage(task.task_id, "embedding")

        from comet_rag.tasks import TaskError

        await store.transition(
            task.task_id,
            TaskStatus.PENDING,
            error=TaskError(code="boom", message="上游 503", retriable=True),
        )

        history = (await store.require(task.task_id)).stage_history
        assert history[-1].status == "failed"
        assert history[-1].finished_at is not None

    # ── 事件流 ─────────────────────────────────────────────────────────────

    async def test_events_are_sequential(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)
        await store.enter_stage(task.task_id, "one")

        events = await store.events(task.task_id)
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert events[0].type == "created"

    async def test_events_after_seq_filters(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)

        events = await store.events(task.task_id)
        tail = await store.events(task.task_id, after_seq=events[0].seq)
        assert all(e.seq > events[0].seq for e in tail)
        assert len(tail) == len(events) - 1

    async def test_events_are_scoped_per_task(self, store: TaskStore) -> None:
        a = await store.create("demo")
        b = await store.create("demo")

        assert all(e.task_id == a.task_id for e in await store.events(a.task_id))
        assert all(e.task_id == b.task_id for e in await store.events(b.task_id))

    # ── 心跳与租约 ─────────────────────────────────────────────────────────

    async def test_heartbeat_does_not_bump_version(self, store: TaskStore) -> None:
        """心跳几秒一次，若涨版本会把所有 runner 的 expected_version 撞失效，
        乐观锁退化成「不停重试」。语义上心跳不是任务状态的变更。"""
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)
        before = (await store.require(task.task_id)).version

        await store.heartbeat(task.task_id)

        assert (await store.require(task.task_id)).version == before

    async def test_heartbeat_advances_timestamp(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)
        await store.update(task.task_id, heartbeat_at=Time.now() - timedelta(minutes=1))
        old = (await store.require(task.task_id)).heartbeat_at

        await store.heartbeat(task.task_id)

        new = (await store.require(task.task_id)).heartbeat_at
        assert old is not None and new is not None and new > old

    async def test_sweep_requeues_zombie_with_attempts_left(
        self, store: TaskStore
    ) -> None:
        task = await store.create("demo", max_attempts=2)
        await store.transition(
            task.task_id, TaskStatus.RUNNING, attempts=1, worker_id="dead"
        )
        await store.update(task.task_id, heartbeat_at=Time.now() - timedelta(minutes=5))

        revived = await store.sweep_stale(lease=timedelta(seconds=30))

        assert revived == [task.task_id]
        recovered = await store.require(task.task_id)
        assert recovered.status is TaskStatus.PENDING
        assert recovered.error is not None
        assert recovered.error.code == "lease_expired"

    async def test_sweep_fails_zombie_without_attempts_left(
        self, store: TaskStore
    ) -> None:
        task = await store.create("demo", max_attempts=1)
        await store.transition(
            task.task_id, TaskStatus.RUNNING, attempts=1, worker_id="dead"
        )
        await store.update(task.task_id, heartbeat_at=Time.now() - timedelta(minutes=5))

        await store.sweep_stale(lease=timedelta(seconds=30))

        assert (await store.require(task.task_id)).status is TaskStatus.FAILED

    async def test_sweep_leaves_live_tasks_alone(self, store: TaskStore) -> None:
        """误回收会造成一份任务两个执行者 —— 比不回收更糟。"""
        task = await store.create("demo", max_attempts=2)
        await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="alive")

        assert await store.sweep_stale(lease=timedelta(seconds=30)) == []
        assert (await store.require(task.task_id)).status is TaskStatus.RUNNING

    async def test_sweep_ignores_non_running_tasks(self, store: TaskStore) -> None:
        task = await store.create("demo", max_attempts=2)
        await store.update(task.task_id, heartbeat_at=Time.now() - timedelta(minutes=5))

        assert await store.sweep_stale(lease=timedelta(seconds=30)) == []

    async def test_sweep_finalizes_stale_cancelling_tasks(
        self, store: TaskStore
    ) -> None:
        """**CANCELLING 也必须被回收**（PR 评审 #2 指出的缺口）。

        取消是协作式的：先写 CANCELLING，再等 runner 走到 `ctx.checkpoint()`
        自己退出并落 CANCELLED。worker 若在这中间死掉，那一步永远不会发生 ——
        任务卡在 CANCELLING：既不是终态、没人再推进、也不会被回收，
        而它恰恰是**用户已经明确要求停掉**的那一个。
        """
        task = await store.create("demo", max_attempts=3)
        await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="dead")
        await store.transition(task.task_id, TaskStatus.CANCELLING)
        await store.update(task.task_id, heartbeat_at=Time.now() - timedelta(minutes=5))

        revived = await store.sweep_stale(lease=timedelta(seconds=30))

        assert revived == [task.task_id], "卡在 CANCELLING 的任务没被回收"
        done = await store.require(task.task_id)
        # 直接给终态而不是重排队：用户要的是"停下来"，重跑等于违背原意
        assert done.status is TaskStatus.CANCELLED
        assert done.finished_at is not None
        assert done.error is not None
        assert done.error.code == "cancelled_lease_expired"

    async def test_sweep_leaves_live_cancelling_tasks_alone(
        self, store: TaskStore
    ) -> None:
        """心跳正常的 CANCELLING 说明 runner 还活着，正在往检查点走。"""
        task = await store.create("demo", max_attempts=3)
        await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="alive")
        await store.transition(task.task_id, TaskStatus.CANCELLING)

        assert await store.sweep_stale(lease=timedelta(seconds=30)) == []
        assert (await store.require(task.task_id)).status is TaskStatus.CANCELLING

    # ── 删除 ───────────────────────────────────────────────────────────────

    async def test_delete_idle_task(self, store: TaskStore) -> None:
        task = await store.create("demo")

        assert await store.delete(task.task_id) is True
        assert await store.get(task.task_id) is None

    async def test_delete_unknown_returns_false(self, store: TaskStore) -> None:
        assert await store.delete("不存在") is False

    async def test_delete_running_task_is_refused(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)

        with pytest.raises(TaskBusy):
            await store.delete(task.task_id)

    async def test_force_delete_running_task(self, store: TaskStore) -> None:
        task = await store.create("demo")
        await store.transition(task.task_id, TaskStatus.RUNNING)

        assert await store.delete(task.task_id, force=True) is True
        assert await store.get(task.task_id) is None

    # ── 序列化 ─────────────────────────────────────────────────────────────

    async def test_round_trip_is_lossless(self, store: TaskStore) -> None:
        """跨进程恢复的前提。任何实现落库时都要过这一关。"""
        task = await store.create("demo", request={"topic": "x"}, max_attempts=3)
        await store.transition(task.task_id, TaskStatus.RUNNING, worker_id="w1")
        await store.enter_stage(task.task_id, "one")
        await store.update(task.task_id, context={"n": 1}, progress=0.3)

        fresh = await store.require(task.task_id)
        assert Task.from_dict(fresh.to_dict()) == fresh
