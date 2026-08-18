"""任务态存储：**只管状态，不管调度**。

与原稿最大的区别：`spawn / cancel` 被移出去了（见 executor.py）。
理由很实在——`spawn(task, coro)` 收的是协程对象，Redis 里没地方放协程；
一旦把它写进「换实现时签名不变」的接口，这句承诺就是假的。
拆开后还顺带解决了两件事：协程对象丢了任务就没法重跑、进程重启后无法恢复。

`TaskStore` 用 **ABC 而非 Protocol**：真正会变的只有 6 个存取原语，
`create / update / transition / sweep_stale` 这些含业务规则的逻辑（状态机守卫、
时间戳、事件留痕）应当由基类统一实现，换 Redis 时不该、也不能重写一遍。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from comet_rag.core.logging import logger
from comet_rag.core.time import Time

from .models import (
    FIELD_NAMES,
    StageRecord,
    Task,
    TaskError,
    TaskEvent,
    TaskStatus,
    new_task_id,
)
from .states import InvalidTransition, assert_transition


class TaskNotFound(LookupError):
    pass


class VersionConflict(RuntimeError):
    """乐观锁冲突：期间有别人写过这条记录。调用方应重读后重试。"""


class TaskBusy(RuntimeError):
    """对在跑的任务做了只能对静止任务做的操作（如 delete）。"""


# 这些字段不许经 update() 改：status 必须走 transition()，其余是不可变身份
_IMMUTABLE = frozenset({"task_id", "kind", "created_at", "version", "status"})

#: 内部 read-modify-write 撞版本时的重读次数。冲突本就罕见（同一任务同时
#: 只有一个 runner 在推进），几次足够；再多只是把真正的死锁拖成慢性病。
_CAS_RETRIES = 5


class TaskStore(ABC):
    """任务态存储。抽象类，子类需要实现以下7个方法。"""

    # 抽象方法
    @abstractmethod
    async def _insert(self, task: Task) -> tuple[Task, bool]:
        """原子插入，返回 ``(落库任务, 是否新建)``。

        带幂等键的并发插入必须让一个写入者获胜，其余返回赢家；不能把唯一
        约束冲突泄漏成 500。
        """

    @abstractmethod
    async def _load(self, task_id: str) -> Task | None:
        """返回**副本**。返回活对象会让调用方绕过 CAS 直接改库。"""

    @abstractmethod
    async def _save(
        self, task: Task, expected_version: int, *, bump: bool = True
    ) -> Task:
        """CAS 写入：版本不符抛 VersionConflict；成功则 version += 1。

        `bump=False` 用于**纯运维写入**（目前只有心跳）：它每隔几秒就要写一次，
        若也涨版本号，会把所有 runner 手里的 expected_version 撞失效，
        乐观锁退化成「不停重试」。语义上心跳不属于任务状态的变更。
        """

    @abstractmethod
    async def _remove(self, task_id: str) -> bool: ...

    @abstractmethod
    async def _query(
        self,
        *,
        kind: str | None = None,
        status: TaskStatus | None = None,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]: ...

    @abstractmethod
    async def _append_event(
        self,
        task_id: str,
        type: str,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TaskEvent: ...

    @abstractmethod
    async def events(self, task_id: str, *, after_seq: int = 0) -> list[TaskEvent]: ...

    # 模板方法（所有实现共用）
    async def create(
        self,
        kind: str,
        *,
        request: Any = None,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 1,
        **fields: Any,
    ) -> Task:
        """创建任务。带 idempotency_key 时重复提交返回既有任务，不新建。"""
        task = Task(
            task_id=new_task_id(),
            kind=kind,
            request=request,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            **fields,
        )
        persisted, created = await self._insert(task)
        if created:
            await self._append_event(
                persisted.task_id, "created", f"任务已创建（{kind}）"
            )
        return persisted

    async def get(self, task_id: str) -> Task | None:
        return await self._load(task_id)

    async def require(self, task_id: str) -> Task:
        task = await self._load(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        return task

    async def list_tasks(
        self,
        *,
        kind: str | None = None,
        status: TaskStatus | None = None,
        owner_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """带过滤与分页。"""
        return await self._query(
            kind=kind, status=status, owner_id=owner_id, limit=limit, offset=offset
        )

    async def _cas(
        self,
        task_id: str,
        expected_version: int | None,
        mutate: Callable[[Task], Any],
        *,
        bump: bool = True,
    ) -> tuple[Task, Any]:
        """读—改—写，**撞版本就重读重来**。返回 (落库后的任务, mutate 的返回值)。

        这里有个必须说清的区分，它是跨进程部署下的一处真实 bug 的修复
        （T22 集成测试逼出来的）：

          * 调用方**没传** `expected_version` —— CAS 只是本方法内部
            "读出来—改—写回去"的护栏，防的是丢失更新。撞上了就该重读重来，
            把冲突抛给调用方毫无意义：它压根没做过任何版本假设。
          * 调用方**传了** `expected_version` —— 它在断言"我要的就是这一版"
            （比如 API 层的条件更新）。这种冲突必须原样抛出，吞掉就等于
            悄悄覆盖了别人的写入。

        进程内跑不出这个 bug：InMemoryTaskStore 的读写之间没有真正的 await
        间隙。换成 Postgres + 跨进程后窗口宽到必然撞上 —— runner 一边
        `enter_stage`，另一边有人请求取消，两个写入就撞了。

        `mutate` 必须是**纯内存且可重复执行**的：它每轮都拿到一份新快照。
        """
        conflict: VersionConflict | None = None
        for _ in range(_CAS_RETRIES):
            task = await self.require(task_id)
            self._check_version(task, expected_version)
            extra = mutate(task)
            try:
                saved = await self._save(task, expected_version=task.version, bump=bump)
            except VersionConflict as exc:
                if expected_version is not None:
                    raise  # 调用方的版本假设，不能替它重试
                conflict = exc
                continue
            return saved, extra
        raise conflict  # type: ignore[misc]  —— 循环至少跑一轮，必然已赋值

    async def update(
        self, task_id: str, *, expected_version: int | None = None, **fields: Any
    ) -> Task:
        """改非状态字段。传 expected_version 即启用乐观锁。"""
        self._check_fields(fields)

        def mutate(task: Task) -> None:
            for k, v in fields.items():
                setattr(task, k, v)
            task.updated_at = Time.now()

        saved, _ = await self._cas(task_id, expected_version, mutate)
        return saved

    async def transition(
        self,
        task_id: str,
        to: TaskStatus,
        *,
        expected_version: int | None = None,
        note: str | None = None,
        **fields: Any,
    ) -> Task:
        """**唯一**能改 status 的入口：先过状态机守卫，再统一维护时间戳与事件。

        守卫在 `_cas` 的每一轮里重新过一遍 —— 重读之后状态可能已经变了，
        拿旧状态判定过的迁移未必还合法。
        """
        self._check_fields(fields)

        def mutate(task: Task) -> TaskStatus:
            frm = task.status
            assert_transition(frm, to)

            for k, v in fields.items():
                setattr(task, k, v)
            task.status = to
            now = Time.now()
            task.updated_at = now
            if to is TaskStatus.RUNNING:
                task.started_at = task.started_at or now
                task.finished_at = None
                task.heartbeat_at = now
            elif to is TaskStatus.PENDING:
                task.finished_at = None
                task.heartbeat_at = None
                task.worker_id = None
                # 带着错误退回排队 = 可重试失败或租约回收。此时当前阶段的留痕
                # 还开着口，必须在这里收成 failed；否则续跑时 enter_stage 会
                # 把这条失败记录关成 "succeeded"，阶段历史就骗人了。
                if task.error is not None:
                    self._close_stage(task, "failed")
            elif to.is_terminal:
                task.finished_at = now
                task.heartbeat_at = None
                self._close_stage(
                    task, "succeeded" if to is TaskStatus.SUCCEEDED else to.value
                )
                if to is TaskStatus.SUCCEEDED:
                    task.progress = 1.0
            return frm

        saved, frm = await self._cas(task_id, expected_version, mutate)
        await self._append_event(
            task_id,
            "transition",
            note or f"{frm.value} → {to.value}",
            {"from": frm.value, "to": to.value, "stage": saved.stage},
        )
        return saved

    async def heartbeat(self, task_id: str) -> None:
        """续租。跨进程部署时，靠它 + sweep_stale 回收崩溃 worker 留下的僵尸任务。"""

        def mutate(task: Task) -> None:
            task.heartbeat_at = Time.now()

        await self._cas(task_id, None, mutate, bump=False)

    async def sweep_stale(self, lease: timedelta) -> list[str]:
        """回收心跳超时的任务：还有重试次数就退回排队，否则判失败。

        这段逻辑对内存/Redis/DB 完全一致 —— 正是它让 TaskStore 值得做成 ABC 而非 Protocol。

        注意：它是给**跨进程**部署兜底的（worker 崩了，没人再写心跳）。单进程模式下
        协程还活着，回收反而会造成一份任务两个执行者，所以单机不要挂这个定时器。

        **CANCELLING 也要扫**（评审指出的缺口）。取消是协作式的：先把状态写成
        CANCELLING，再等 runner 走到 `ctx.checkpoint()` 自己退出并落 CANCELLED。
        worker 若在这中间死掉，那一步就永远不会发生 —— 任务卡在 CANCELLING，
        既不是终态、也没人再推进它，而它恰恰是**用户已经明确要求停掉**的任务。
        """
        now, revived = Time.now(), []
        for task in await self._stale_candidates(lease, now):
            try:
                await self._reclaim(task)
            except (InvalidTransition, TaskNotFound) as exc:
                # 竞态，不是错误：候选选出来之后、回收动手之前，worker 自己把
                # 任务推进到了终态（或它被删了）。回收本就是给"没人再推进它"
                # 的任务兜底，这里恰恰说明不需要兜底。
                #
                # 关键在于**不能让它中断整轮扫描**。一个碰巧完成的任务若把异常
                # 抛出去，同一批里其余的僵尸任务一个都回收不到 —— 而扫描正是
                # 它们唯一的指望。下一轮很可能再撞上同样的竞态，于是僵尸任务
                # 一直躺着，日志里却只有一条看不出所以然的迁移错误。
                logger.debug(f"跳过 {task.task_id}：回收前状态已变（{exc!r}）")
                continue
            revived.append(task.task_id)
        return revived

    async def _reclaim(self, task: Task) -> None:
        """回收单个心跳超时的任务。竞态由调用方 `sweep_stale` 兜住。"""
        if task.status is TaskStatus.CANCELLING:
            # 用户要的是"停下来"，那就直接给他终态；重排队等于违背原意
            await self.transition(
                task.task_id,
                TaskStatus.CANCELLED,
                error=TaskError(
                    code="cancelled_lease_expired",
                    message="worker 在处理取消的过程中失联",
                ),
                note="租约过期（取消中）",
            )
            return

        err = TaskError(code="lease_expired", message="worker 心跳超时", retriable=True)
        nxt = (
            TaskStatus.PENDING
            if task.attempts < task.max_attempts
            else TaskStatus.FAILED
        )
        await self.transition(task.task_id, nxt, error=err, note="租约过期")

    async def _stale_candidates(
        self, lease: timedelta, now: datetime
    ) -> list[Task]:
        """所有心跳已超时的**活跃**任务。

        活跃 = RUNNING 或 CANCELLING，也就是 `TaskStatus.is_active` 的定义。
        分两次查是因为 `_query` 一次只收一个 status；这里任务量小（上限 1000），
        不值得为它给存储层加一个多状态查询原语。
        """
        candidates: list[Task] = []
        for status in (TaskStatus.RUNNING, TaskStatus.CANCELLING):
            for task in await self._query(status=status, limit=1000):
                beat = task.heartbeat_at or task.started_at or task.updated_at
                if now - beat > lease:
                    candidates.append(task)
        return candidates

    async def delete(self, task_id: str, *, force: bool = False) -> bool:
        """删除。原稿对 RUNNING 任务的行为是未定义的，这里显式规定：

        默认拒绝删在跑的任务（应先 `TaskService.cancel()` 等它停）；
        `force=True` 才允许硬删，此时执行器那边的协程会在下一个检查点自然消亡。
        """
        task = await self._load(task_id)
        if task is None:
            return False
        if task.status.is_active and not force:
            raise TaskBusy(
                f"任务 {task_id} 正在执行（{task.status.value}），请先取消或 force=True"
            )
        return await self._remove(task_id)

    # 阶段留痕
    async def enter_stage(self, task_id: str, stage: str, **fields: Any) -> Task:
        """进入新阶段：关掉上一条阶段记录，开一条新的。"""
        self._check_fields(fields)

        def mutate(task: Task) -> None:
            self._close_stage(task, "succeeded")
            task.stage = stage
            task.stage_history.append(StageRecord(stage=stage, started_at=Time.now()))
            for k, v in fields.items():
                setattr(task, k, v)
            task.updated_at = Time.now()

        saved, _ = await self._cas(task_id, None, mutate)
        await self._append_event(
            task_id, "stage", f"进入阶段：{stage}", {"stage": stage}
        )
        return saved

    @staticmethod
    def _close_stage(task: Task, status: str) -> None:
        if task.stage_history and task.stage_history[-1].finished_at is None:
            rec = task.stage_history[-1]
            rec.finished_at = Time.now()
            rec.status = status

    # 内部校验
    @staticmethod
    def _check_fields(fields: dict[str, Any]) -> None:
        bad = set(fields) & _IMMUTABLE
        if bad:
            raise ValueError(
                f"字段不可直接修改：{sorted(bad)}（status 请走 transition）"
            )
        unknown = set(fields) - FIELD_NAMES
        if unknown:
            raise ValueError(f"Task 没有这些字段：{sorted(unknown)}")

    @staticmethod
    def _check_version(task: Task, expected: int | None) -> None:
        if expected is not None and expected != task.version:
            raise VersionConflict(
                f"任务 {task.task_id} 版本 {task.version} ≠ 期望 {expected}"
            )


# 内存实现
def _clone(task: Task) -> Task:
    """拷出一份**独立**快照。真实 DB 实现这里是一次序列化/反序列化。

    `request`、`result` 与 `context` 里的嵌套值都必须真拷。原来只做
    `dict(task.context)` 这种浅拷、且完全没碰 `request`/`result`，于是：

        got = await store.get(task_id)
        got.request["source"] = "别的东西"     # 没有 save、没有 CAS

    这行就**直接改到了库里**：version 不涨、不留事件、别人手上的快照当场
    与库不一致，而乐观锁一无所知 —— 它守的是 `_save`，这条路根本没经过
    `_save`。`_load` 的文档说"返回副本，返回活对象会让调用方绕过 CAS 直接
    改库"，而它自己就是那个活对象。

    `request` 与 `result` 声明成 `Any`，实际装的几乎总是 dict（入库请求、
    结果摘要），正是最容易被顺手改一下的两个字段。

    deepcopy 对不可变值（str、int、datetime）会原样返回，所以代价与容器的
    **元素个数**成正比，与内容大小无关；chunk 列表那种场景不会因此变慢。
    """
    copy = replace(task)
    copy.context = deepcopy(task.context)
    copy.request = deepcopy(task.request)
    copy.result = deepcopy(task.result)
    copy.stage_history = [replace(r) for r in task.stage_history]
    return copy


class InMemoryTaskStore(TaskStore):
    """进程内任务表（重启即丢）。仅用于开发/测试/单机。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, list[TaskEvent]] = {}
        self._lock = asyncio.Lock()

    async def _insert(self, task: Task) -> tuple[Task, bool]:
        async with self._lock:
            if task.idempotency_key:
                for existing in self._tasks.values():
                    if (
                        existing.kind == task.kind
                        and existing.idempotency_key == task.idempotency_key
                    ):
                        return _clone(existing), False
            self._tasks[task.task_id] = _clone(task)
            return _clone(task), True

    async def _load(self, task_id: str) -> Task | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            return _clone(task) if task else None

    async def _save(
        self, task: Task, expected_version: int, *, bump: bool = True
    ) -> Task:
        async with self._lock:
            current = self._tasks.get(task.task_id)
            if current is None:
                raise TaskNotFound(task.task_id)
            if current.version != expected_version:
                raise VersionConflict(
                    f"任务 {task.task_id} 版本 {current.version} ≠ 期望 {expected_version}"
                )
            task.version = expected_version + 1 if bump else expected_version
            self._tasks[task.task_id] = _clone(task)
            return _clone(task)

    async def _remove(self, task_id: str) -> bool:
        async with self._lock:
            self._events.pop(task_id, None)
            return self._tasks.pop(task_id, None) is not None

    async def _query(
        self,
        *,
        kind: str | None = None,
        status: TaskStatus | None = None,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        async with self._lock:
            rows = [
                t
                for t in self._tasks.values()
                if (kind is None or t.kind == kind)
                and (status is None or t.status is status)
                and (owner_id is None or t.owner_id == owner_id)
                and (idempotency_key is None or t.idempotency_key == idempotency_key)
            ]
        rows.sort(key=lambda t: t.created_at, reverse=True)
        return [_clone(t) for t in rows[offset : offset + limit]]

    async def _append_event(
        self,
        task_id: str,
        type: str,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        async with self._lock:
            bucket = self._events.setdefault(task_id, [])
            event = TaskEvent(
                task_id=task_id,
                seq=len(bucket) + 1,
                at=Time.now(),
                type=type,
                message=message,
                data=data or {},
            )
            bucket.append(event)
            return event

    async def events(self, task_id: str, *, after_seq: int = 0) -> list[TaskEvent]:
        async with self._lock:
            return [e for e in self._events.get(task_id, []) if e.seq > after_seq]
