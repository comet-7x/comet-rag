"""`TaskStore` 的 PostgreSQL 实现。

只实现 7 个存取原语，业务规则（状态机守卫、时间戳维护、租约回收、事件留痕）
全在基类的模板方法里 —— 换存储时**不该也不能**重写一遍。这正是 `TaskStore`
做成 ABC 而非 Protocol 的理由。

## 两处并发难点，用了两种不同的手段

**乐观锁**用 `UPDATE ... WHERE version = :expected` 一条语句完成比较与写入。
写成"先 SELECT 比对再 UPDATE"是错的：两步之间有窗口，两个请求可能都读到
同一个版本号然后都认为自己该赢。这里适合乐观锁，因为冲突**罕见**——
同一个任务同时只会有一个 runner 在推进它。

**事件序号**用 `SELECT ... FOR UPDATE` 悲观锁住父任务行再分配。
最初也想用乐观思路（子查询取 `max(seq)+1`，主键冲突就重试），
12 路并发下直接崩了：所有写入者都在抢同一个号，冲突是**必然**而非偶然，
重试次数加多少都只是把失败概率往后推。

两处的取舍不同，是因为冲突概率的量级不同 —— 乐观锁适合"几乎不冲突"，
悲观锁适合"必然冲突"。搞反了两边都会很难受。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from comet_rag.infrastructure.database.models import TaskEventRow, TaskRow
from comet_rag.infrastructure.database.session import Database, affected_rows
from comet_rag.tasks.models import Task, TaskEvent, TaskStatus, Time
from comet_rag.tasks.store import TaskNotFound, TaskStore, VersionConflict

#: 这些字段在 Task 上是复杂结构，落库走 JSON 列。
_JSON_FIELDS = ("request", "context", "result", "stage_history", "error")

#: 这些是标量/时间列，直接从 Task 取原值（不经 to_dict 的 ISO 编码）。
_SCALAR_FIELDS = (
    "kind",
    "owner_id",
    "idempotency_key",
    "stage",
    "result_uri",
    "resume_stage",
    "attempts",
    "max_attempts",
    "progress",
    "message",
    "worker_id",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "heartbeat_at",
)


def _to_columns(task: Task) -> dict[str, Any]:
    """Task → 列值。

    JSON 字段借用 `Task.to_dict()` 的编码 —— 那套编码已被序列化往返测试
    覆盖，重写一遍只会多一份需要同步维护的真相。
    """
    encoded = task.to_dict()
    values: dict[str, Any] = {name: getattr(task, name) for name in _SCALAR_FIELDS}
    values["status"] = task.status.value
    for name in _JSON_FIELDS:
        values[name] = encoded[name]
    return values


def _to_task(row: TaskRow) -> Task:
    """列值 → Task。走 `Task.from_dict()`，与写入端共用同一套编解码。"""
    payload: dict[str, Any] = {
        "task_id": row.task_id,
        "status": row.status,
        "version": row.version,
    }
    for name in _SCALAR_FIELDS:
        payload[name] = getattr(row, name)
    for name in _JSON_FIELDS:
        payload[name] = getattr(row, name)
    return Task.from_dict(payload)


class PostgresTaskStore(TaskStore):
    def __init__(self, database: Database) -> None:
        self._db = database

    # ── 存取原语 ───────────────────────────────────────────────────────────

    async def _insert(self, task: Task) -> tuple[Task, bool]:
        try:
            async with self._db.transaction() as session:
                session.add(
                    TaskRow(task_id=task.task_id, version=0, **_to_columns(task))
                )
        except IntegrityError:
            # 唯一约束是最终裁判：两个请求即便同时预读到“不存在”，也只有
            # 一个能插入。事务回滚后读取赢家，返回与顺序重复提交相同的结果。
            if task.idempotency_key:
                existing = await self._query(
                    kind=task.kind,
                    idempotency_key=task.idempotency_key,
                    limit=1,
                )
                if existing:
                    return existing[0], False
            raise
        return task, True

    async def _load(self, task_id: str) -> Task | None:
        async with self._db.session() as session:
            row = await session.get(TaskRow, task_id)
            # 每次都重新构造对象，天然是副本 —— 调用方改它不会影响库
            return _to_task(row) if row is not None else None

    async def _save(
        self, task: Task, expected_version: int, *, bump: bool = True
    ) -> Task:
        next_version = expected_version + 1 if bump else expected_version
        values = {**_to_columns(task), "version": next_version}

        async with self._db.transaction() as session:
            # 比较与写入必须在同一条语句里。分成"先查后写"会留出窗口，
            # 两个请求可能都读到同一个版本号、都认为自己该赢。
            result = await session.execute(
                update(TaskRow)
                .where(
                    TaskRow.task_id == task.task_id,
                    TaskRow.version == expected_version,
                )
                .values(**values)
            )
            if affected_rows(result) == 0:
                # 没更新到：要么任务不存在，要么版本被别人改过。区分开来，
                # 因为调用方对两者的处理完全不同（放弃 vs 重读后重试）。
                current = await session.get(TaskRow, task.task_id)
                if current is None:
                    raise TaskNotFound(task.task_id)
                raise VersionConflict(
                    f"任务 {task.task_id} 版本 {current.version} ≠ 期望 {expected_version}"
                )

        task.version = next_version
        return await self.require(task.task_id)

    async def _remove(self, task_id: str) -> bool:
        async with self._db.transaction() as session:
            result = await session.execute(
                delete(TaskRow).where(TaskRow.task_id == task_id)
            )
            return bool(affected_rows(result))

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
        stmt = select(TaskRow).order_by(TaskRow.created_at.desc())
        if kind is not None:
            stmt = stmt.where(TaskRow.kind == kind)
        if status is not None:
            stmt = stmt.where(TaskRow.status == status.value)
        if owner_id is not None:
            stmt = stmt.where(TaskRow.owner_id == owner_id)
        if idempotency_key is not None:
            stmt = stmt.where(TaskRow.idempotency_key == idempotency_key)

        async with self._db.session() as session:
            rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars()
            return [_to_task(row) for row in rows]

    async def _append_event(
        self,
        task_id: str,
        type: str,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        """追加事件。序号在**父任务行的排他锁**下分配。

        最初写成"子查询取 max(seq)+1，主键冲突就重试"，12 路并发下直接崩了 ——
        冲突会级联，重试次数再加也只是把失败概率往后推。根因是那个方案里
        并发写入者全都在抢同一个号，冲突是**必然**而非偶然。

        改成先 `SELECT ... FOR UPDATE` 锁住父任务行：同一任务的事件写入被
        串行化，序号分配不再有竞争；不同任务之间照样并行，因为锁的粒度是行。
        实际负载下这几乎没有代价 —— 一个任务同时只会有一个 runner 在推进它。
        """
        now = Time.now()
        async with self._db.transaction() as session:
            locked = (
                await session.execute(
                    select(TaskRow.task_id)
                    .where(TaskRow.task_id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                raise TaskNotFound(task_id)

            next_seq = (
                await session.execute(
                    select(func.coalesce(func.max(TaskEventRow.seq), 0) + 1).where(
                        TaskEventRow.task_id == task_id
                    )
                )
            ).scalar_one()
            session.add(
                TaskEventRow(
                    task_id=task_id,
                    seq=next_seq,
                    at=now,
                    type=type,
                    message=message,
                    data=data or {},
                )
            )

        return TaskEvent(
            task_id=task_id,
            seq=next_seq,
            at=now,
            type=type,
            message=message,
            data=data or {},
        )

    async def events(self, task_id: str, *, after_seq: int = 0) -> list[TaskEvent]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(TaskEventRow)
                    .where(
                        TaskEventRow.task_id == task_id,
                        TaskEventRow.seq > after_seq,
                    )
                    .order_by(TaskEventRow.seq)
                )
            ).scalars()
            return [
                TaskEvent(
                    task_id=row.task_id,
                    seq=row.seq,
                    at=row.at,
                    type=row.type,
                    message=row.message,
                    data=row.data or {},
                )
                for row in rows
            ]

    # ── 运维 ───────────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        async with self._db.session() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one() == 1


__all__ = ["PostgresTaskStore"]
