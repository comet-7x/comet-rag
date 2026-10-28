from __future__ import annotations

import pytest

from comet_rag.services.flat_write import (
    FLAT_WRITE_KIND,
    flat_source_write,
    guard_flat_write_deletion,
)
from comet_rag.tasks import InMemoryTaskStore, TaskBusy, TaskContext, TaskStatus


async def context(store):
    task = await store.create("ingest")
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="worker", attempts=1
    )
    return TaskContext(store, task.task_id, worker_id="worker", attempts=1)


async def test_independent_contexts_cannot_write_one_source_concurrently():
    store = InMemoryTaskStore()
    first, second = await context(store), await context(store)
    async with flat_source_write(first, "kb", "source"):
        with pytest.raises(TaskBusy, match="并发覆盖"):
            async with flat_source_write(second, "kb", "source"):
                pytest.fail("第二个写者不应进入")
        async with flat_source_write(second, "other-kb", "source"):
            pass
    async with flat_source_write(second, "kb", "source"):
        pass
    records = await store.list_tasks(kind=FLAT_WRITE_KIND)
    assert len(records) == 2 and all(r.context == {"writer": None} for r in records)
    assert all(r.status is TaskStatus.CANCELLED for r in records)


async def test_unknown_rpc_keeps_source_fence_even_after_owner_stops():
    store = InMemoryTaskStore()
    first, second = await context(store), await context(store)

    async def unknown():
        raise TimeoutError("server may still commit")

    with pytest.raises(TimeoutError):
        async with flat_source_write(first, "kb", "source") as permit:
            await permit.write(unknown())
    await store.transition(first.task_id, TaskStatus.FAILED)
    with pytest.raises(TaskBusy):
        async with flat_source_write(second, "kb", "source"):
            pytest.fail("不能用本地失败推断远端排干")
    (record,) = await store.list_tasks(kind=FLAT_WRITE_KIND)
    assert record.context["writer"]["task_id"] == first.task_id
    with pytest.raises(TaskBusy):
        await store.delete(record.task_id, force=True, guard=guard_flat_write_deletion)


async def test_pre_rpc_failure_releases_fence_without_claiming_atomic_rollback():
    store = InMemoryTaskStore()
    ctx = await context(store)
    with pytest.raises(ValueError):
        async with flat_source_write(ctx, "kb", "source"):
            raise ValueError("embedding rejected before vector RPC")
    async with flat_source_write(ctx, "kb", "source"):
        pass


async def test_claim_commit_reply_loss_never_authorizes_write(monkeypatch):
    store = InMemoryTaskStore()
    ctx = await context(store)
    original = store._put_context

    async def lose(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("claim reply lost")

    monkeypatch.setattr(store, "_put_context", lose)
    with pytest.raises(OSError):
        async with flat_source_write(ctx, "kb", "source"):
            pytest.fail("没有拿到确认不应写入")
    monkeypatch.setattr(store, "_put_context", original)
    with pytest.raises(TaskBusy):
        async with flat_source_write(ctx, "kb", "source"):
            pytest.fail("不能猜测丢失的认领是否生效")
