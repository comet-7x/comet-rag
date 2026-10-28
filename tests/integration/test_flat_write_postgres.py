from __future__ import annotations

import pytest

from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.task_store.postgres import PostgresTaskStore
from comet_rag.services.flat_write import FLAT_WRITE_KIND, flat_source_write
from comet_rag.services.task_retention import guard_task_deletion
from comet_rag.tasks import TaskBusy, TaskContext, TaskStatus
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration


async def claim(store, worker):
    task = await store.create("ingest")
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id=worker, attempts=1
    )
    return TaskContext(store, task.task_id, worker_id=worker, attempts=1)


@pytest.mark.parametrize("uncertain", [False, True])
async def test_source_fence_serializes_independent_connections_and_retains_unknown_write(
    database, postgres_dsn, uncertain
):
    await truncate_tables(database, "tasks")
    other = Database(postgres_dsn)
    try:
        first, second = (
            await claim(PostgresTaskStore(database), "first"),
            await claim(PostgresTaskStore(other), "second"),
        )

        async def rpc():
            raise TimeoutError("remote write may commit later")

        async def write():
            async with flat_source_write(first, "kb", "source") as permit:
                with pytest.raises(TaskBusy):
                    async with flat_source_write(second, "kb", "source"):
                        pytest.fail("相同来源不能由两个连接并发写")
                if uncertain:
                    await permit.write(rpc())

        if uncertain:
            with pytest.raises(TimeoutError):
                await write()
            with pytest.raises(TaskBusy):
                async with flat_source_write(second, "kb", "source"):
                    pytest.fail("未知RPC不能用旧执行者退出作为排干证明")
        else:
            await write()
            async with flat_source_write(second, "kb", "source"):
                pass
        (row,) = await second.store.list_tasks(kind=FLAT_WRITE_KIND)
        with pytest.raises(TaskBusy):
            await second.store.delete(
                row.task_id, force=True, guard=guard_task_deletion
            )
    finally:
        await other.close()
