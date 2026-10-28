from __future__ import annotations

import asyncio

import pytest

from comet_rag.services.evidence_publication import (
    PERMISSION,
    PUBLICATION_PERMISSION_KIND,
    verify_publication_target,
)
from comet_rag.services.task_retention import guard_task_deletion
from comet_rag.tasks import TaskBusy, TaskStatus
from tests.unit.services.test_mineru_capture import (
    capture_environment as capture_environment,
)


async def test_permission_is_durable_before_native_put_and_retained(
    monkeypatch, capture_environment
):
    env = capture_environment
    original = env.publisher.verify_target
    calls = 0

    async def verify():
        nonlocal calls
        calls += 1
        (record,) = await env.ctx.store.list_tasks(kind=PUBLICATION_PERMISSION_KIND)
        assert record.context[PERMISSION]["state"] == "probing"
        assert record.request["probe_key"] == env.publisher.probe_key
        assert record.request["target_sha256"] == env.publisher.identity_sha256
        await original()

    monkeypatch.setattr(env.publisher, "verify_target", verify)
    identity = await verify_publication_target(
        env.ctx.store, env.publisher, "documents_v1"
    )
    assert identity == await verify_publication_target(
        env.ctx.store, env.publisher, "documents_v1"
    )
    assert calls == 1
    record = await env.ctx.store.get(identity)
    assert record is not None and record.status is TaskStatus.CANCELLED
    assert record.context == {PERMISSION: {"state": "verified"}}
    with pytest.raises(TaskBusy):
        await env.ctx.store.delete(identity, force=True, guard=guard_task_deletion)
    # 库中成功不能授权一个没有实时验收结果的SDK实例。
    env.publisher._verified = False
    with pytest.raises(RuntimeError, match="unverified"):
        await verify_publication_target(env.ctx.store, env.publisher, "documents_v1")
    assert calls == 1


@pytest.mark.parametrize("boundary", ["claim", "verified"])
async def test_lost_cas_reply_never_replays_native_probe(
    monkeypatch, capture_environment, boundary
):
    env = capture_environment
    original = env.ctx.store._put_context
    native = env.publisher.verify_target
    calls = 0

    async def verify():
        nonlocal calls
        calls += 1
        await native()

    async def lose(task_id, values, **kwargs):
        await original(task_id, values, **kwargs)
        state = values[PERMISSION]["state"]
        if state == ("probing" if boundary == "claim" else "verified"):
            raise OSError("CAS committed, response lost")

    monkeypatch.setattr(env.publisher, "verify_target", verify)
    monkeypatch.setattr(env.ctx.store, "_put_context", lose)
    with pytest.raises(OSError):
        await verify_publication_target(env.ctx.store, env.publisher, "documents_v1")
    monkeypatch.setattr(env.ctx.store, "_put_context", original)
    if boundary == "claim":
        with pytest.raises(TaskBusy):
            await verify_publication_target(
                env.ctx.store, env.publisher, "documents_v1"
            )
    else:
        await verify_publication_target(env.ctx.store, env.publisher, "documents_v1")
    assert calls == (0 if boundary == "claim" else 1)


@pytest.mark.parametrize("fault", ["unknown", "cancel", "deadline"])
async def test_unknown_probe_retains_identity_without_retry(
    monkeypatch, capture_environment, fault
):
    env = capture_environment
    started = asyncio.Event()
    calls = 0

    async def verify():
        nonlocal calls
        calls += 1
        started.set()
        if fault == "unknown":
            raise OSError("PUT may have committed")
        await asyncio.Event().wait()

    monkeypatch.setattr(env.publisher, "verify_target", verify)
    operation = asyncio.create_task(
        verify_publication_target(
            env.ctx.store,
            env.publisher,
            "documents_v1",
            timeout_seconds=0.01 if fault == "deadline" else 120,
        )
    )
    await started.wait()
    if fault == "cancel":
        operation.cancel()
    with pytest.raises(
        {
            "unknown": OSError,
            "cancel": asyncio.CancelledError,
            "deadline": TimeoutError,
        }[fault]
    ):
        await operation
    with pytest.raises(TaskBusy):
        await verify_publication_target(env.ctx.store, env.publisher, "documents_v1")
    (record,) = await env.ctx.store.list_tasks(kind=PUBLICATION_PERMISSION_KIND)
    assert record.context[PERMISSION]["state"] == "probing"
    assert record.request["probe_key"] == env.publisher.probe_key
    assert calls == 1


async def test_concurrent_probe_is_exclusive_and_corruption_fails_closed(
    monkeypatch, capture_environment
):
    env = capture_environment
    started, release = asyncio.Event(), asyncio.Event()
    native = env.publisher.verify_target

    async def verify():
        started.set()
        await release.wait()
        await native()

    monkeypatch.setattr(env.publisher, "verify_target", verify)
    first = asyncio.create_task(
        verify_publication_target(env.ctx.store, env.publisher, "documents_v1")
    )
    await started.wait()
    try:
        with pytest.raises(TaskBusy):
            await verify_publication_target(
                env.ctx.store, env.publisher, "documents_v1"
            )
    finally:
        release.set()
        identity = await first
    await env.ctx.store._put_context(
        identity, {PERMISSION: {"state": "unknown"}}, guard=lambda _: None
    )
    with pytest.raises(TaskBusy):
        await verify_publication_target(env.ctx.store, env.publisher, "documents_v1")


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), 121])
async def test_invalid_deadline_has_no_store_or_native_side_effects(
    capture_environment, timeout
):
    env = capture_environment
    with pytest.raises(ValueError):
        await verify_publication_target(
            env.ctx.store, env.publisher, "documents_v1", timeout_seconds=timeout
        )
    assert await env.ctx.store.list_tasks(kind=PUBLICATION_PERMISSION_KIND) == []
