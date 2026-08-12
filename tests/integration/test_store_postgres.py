"""`PostgresTaskStore` 跑与 `InMemoryTaskStore` **同一套**契约。

本文件除 fixture 外只加两条 Postgres 特有的用例（真并发下的 CAS、
以及外键级联）。其余任何断言都该写进契约 —— 写在这里就意味着
"内存实现没被同等要求"，那正是契约测试要消灭的情况。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from comet_rag.infrastructure.database import Database
from comet_rag.tasks import TaskStatus, TaskStore, VersionConflict
from comet_rag.tasks.store_postgres import PostgresTaskStore
from tests.contracts.task_store import TaskStoreContract
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration


async def _require_migrated(database: Database) -> None:
    async with database.session() as session:
        exists = (
            await session.execute(text("SELECT to_regclass('tasks')"))
        ).scalar_one()
    if exists is None:
        pytest.skip("tasks 表不存在，先跑 `uv run alembic upgrade head`")


async def _truncate(database: Database) -> None:
    """CASCADE 一并清掉 task_events。锁等待有上限 —— 见 `conftest.truncate_tables`。"""
    await truncate_tables(database, "tasks")


@pytest.fixture
async def store(database: Database) -> AsyncIterator[TaskStore]:
    await _require_migrated(database)
    # 每个用例前清空：残留数据会让分页、排序类断言随机失败，
    # 且失败与否取决于用例执行顺序 —— 那类问题排查成本极高。
    await _truncate(database)
    yield PostgresTaskStore(database)
    await _truncate(database)


class TestPostgresTaskStore(TaskStoreContract):
    @pytest.fixture
    async def store(self, store: TaskStore) -> TaskStore:  # noqa: PT004
        return store


# ── Postgres 特有 ──────────────────────────────────────────────────────────


class _BarrierStore(PostgresTaskStore):
    """在写入前设一道屏障，强制"所有协程都读完、才有人开始写"的调度。

    没有这道屏障，asyncio 的调度往往让每个事务读→写→提交一气呵成，
    危险窗口根本不出现 —— 测试会通过，但它**没有验证到任何东西**。
    （这一点是靠反向验证发现的：把原子 CAS 换成"先查后写"，
    原来的用例照样全绿。）
    """

    def __init__(self, database: Database, barrier: asyncio.Barrier) -> None:
        super().__init__(database)
        self._barrier = barrier

    async def _save(self, task, expected_version: int, *, bump: bool = True):
        await self._barrier.wait()
        return await super()._save(task, expected_version, bump=bump)


async def test_concurrent_writes_do_not_lose_updates(database: Database) -> None:
    """**丢失更新**：多个写入者都基于同一个旧版本，最后一个把前面的全覆盖掉。

    实测对比（见 commit 说明）：在"所有读先于任一写"的调度下，
    原子 `UPDATE ... WHERE version = :expected` 出 1 个赢家，
    而"先 SELECT 比对再 UPDATE"出 10 个 —— 9 次写入被静默丢弃。
    """
    await _truncate(database)
    concurrency = 10
    barrier = asyncio.Barrier(concurrency)
    store = _BarrierStore(database, barrier)

    task = await store.create("demo")
    version = task.version

    async def write(n: int) -> None:
        await store.update(task.task_id, message=f"w{n}", expected_version=version)

    results = await asyncio.gather(
        *(write(i) for i in range(concurrency)), return_exceptions=True
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, VersionConflict)]
    assert len(winners) == 1, (
        f"{len(winners)} 个写入者都认为自己赢了 —— 说明比较与写入不是原子的，"
        f"有 {len(winners) - 1} 次更新被静默丢弃"
    )
    assert len(conflicts) == concurrency - 1
    assert (await store.require(task.task_id)).version == version + 1


async def test_deleting_task_cascades_its_events(
    store: TaskStore, database: Database
) -> None:
    """不级联的话会留下查不到主体的孤儿事件，越积越多且没人会去清。"""
    task = await store.create("demo")
    await store.transition(task.task_id, TaskStatus.RUNNING)
    assert await store.events(task.task_id)

    await store.delete(task.task_id, force=True)

    async with database.session() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM task_events WHERE task_id = :tid"),
                {"tid": task.task_id},
            )
        ).scalar_one()
    assert remaining == 0


async def test_concurrent_events_get_distinct_sequence_numbers(
    store: TaskStore,
) -> None:
    """事件序号在父任务行的排他锁下分配。并发下不得出现重号或空洞。

    最初用的是"子查询取下一个号、主键冲突就重试"，12 路并发下直接崩了 ——
    所有写入者都在抢同一个号，冲突是必然而非偶然，加重试次数只是把失败
    概率往后推。是本用例逼出的修复。
    """
    task = await store.create("demo")

    await asyncio.gather(
        *(store._append_event(task.task_id, "probe", f"m{i}") for i in range(12))
    )

    events = await store.events(task.task_id)
    seqs = [e.seq for e in events]
    assert len(seqs) == len(set(seqs)), f"出现重号：{seqs}"
    assert seqs == sorted(seqs)
