from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from arq import create_pool
from arq.connections import RedisSettings

from comet_rag.infrastructure.task_execution.arq import ArqExecutor
from comet_rag.ports import VectorRecord
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.services.revision_reconciliation import (
    RevisionReconciler,
    RevisionTaskRetained,
    guard_revision_task_deletion,
)
from comet_rag.tasks import (
    Done,
    RetriableError,
    RunnerRegistry,
    TaskContext,
    TaskService,
    TaskStatus,
)
from tests.contracts.support import wait_for_terminal
from tests.integration.arq_support import _make_worker
from tests.integration.test_revision_lifecycle import _assert_visible
from tests.integration.test_revision_publication_recovery import (
    Environment,
)
from tests.integration.test_revision_publication_recovery import (
    publication_env as publication_env,
)
from tests.integration.worker_lifecycle import stop_workers

pytestmark = pytest.mark.integration


def operation(env: Environment, *, page_size: int = 1) -> RevisionReconciler:
    return RevisionReconciler(
        task_store=env.read_tasks,
        document_store=env.read_documents,
        garbage_collector=RevisionGarbageCollector(
            document_store=env.read_documents, vector_store=env.reader
        ),
        task_kind="revision-publication-test",
        page_size=page_size,
    )


async def finish_source(env: Environment):
    winner = await env.replace()
    await env.stages().activate(env.ctx)
    await env.stages().cleanup(env.ctx)
    await env.tasks.transition(env.ctx.task_id, TaskStatus.SUCCEEDED)
    return winner


async def scanner(env: Environment):
    task = await env.tasks.create("reconciliation-test")
    await env.tasks.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="scan", attempts=1
    )
    return TaskContext(env.read_tasks, task.task_id, worker_id="scan", attempts=1)


async def late_write(env: Environment) -> None:
    child = env.plan.vector_children[0]
    await env.writer.upsert(
        env.plan.key.kb_id,
        [VectorRecord(child.id, child.text, [1.0, 0.0], dict(child.metadata))],
    )


async def test_real_scan_revisits_purged_with_new_context_and_retains_source(
    publication_env: Environment,
) -> None:
    env = publication_env
    winner = await finish_source(env)
    source = await env.read_tasks.require(env.ctx.task_id)
    scan = await scanner(env)
    await operation(env).run_page(scan)
    await late_write(env)
    condition = {"source_id": "source", "document_revision": env.plan.key.revision_id}
    assert await env.reader.count(env.plan.key.kb_id, filter=condition) == 1
    # 模拟扫描者进程失败并由新 attempt 接管，游标仅从 PostgreSQL 取。
    await env.tasks.transition(scan.task_id, TaskStatus.FAILED)
    await env.tasks.transition(scan.task_id, TaskStatus.PENDING)
    await env.tasks.transition(
        scan.task_id, TaskStatus.RUNNING, worker_id="replacement", attempts=2
    )
    fresh = TaskContext(env.tasks, scan.task_id, worker_id="replacement", attempts=2)
    assert (await operation(env).run_page(fresh)).cycle_complete
    report = await operation(env).run_page(fresh)
    assert report.cleaned == (env.ctx.task_id,) and not report.failed
    assert await env.reader.count(env.plan.key.kb_id, filter=condition) == 0
    assert await env.read_tasks.require(env.ctx.task_id) == source
    executor = Mock(request_cancel=AsyncMock())
    service = TaskService(
        env.tasks, executor, delete_guard=guard_revision_task_deletion
    )
    for task_id in (env.ctx.task_id, scan.task_id):
        with pytest.raises(RevisionTaskRetained):
            await service.delete(task_id, force=True)
    executor.request_cancel.assert_not_awaited()
    await _assert_visible(env.service, winner)


@pytest.mark.parametrize("after_commit", [False, True])
async def test_real_cursor_confirmation_loss_recovers_without_republishing(
    publication_env: Environment, after_commit: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = publication_env
    winner = await finish_source(env)
    await late_write(env)
    scan = await scanner(env)
    put = scan.put_bounded

    async def lose(limit, **values):
        if values["revision_scan"]["before"] is not None:
            if after_commit:
                await put(limit, **values)
            raise ConnectionError("cursor response lost")
        await put(limit, **values)

    with monkeypatch.context() as patch:
        patch.setattr(scan, "put_bounded", lose)
        with pytest.raises(ConnectionError):
            await operation(env).run_page(scan)
    saved = await env.tasks.require(scan.task_id)
    assert (saved.context["revision_scan"]["before"] is not None) is after_commit
    fresh = TaskContext(env.tasks, scan.task_id, worker_id="scan", attempts=1)
    result = await operation(env).run_page(fresh)
    assert result.scanned == (0 if after_commit else 1)
    assert (
        await env.reader.count(
            env.plan.key.kb_id, filter={"document_revision": env.plan.key.revision_id}
        )
        == 0
    )
    await _assert_visible(env.service, winner)


@pytest.mark.parametrize("after_commit", [False, True])
async def test_real_arq_retry_restores_persistent_scan_cursor(
    publication_env: Environment,
    redis_url: str,
    after_commit: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = publication_env
    winner = await finish_source(env)
    await late_write(env)
    pool = await create_pool(RedisSettings.from_dsn(redis_url))
    queue = "comet:test:reconcile:" + uuid4().hex
    kind = "reconcile:" + uuid4().hex[:10]
    registry = RunnerRegistry()
    executor = ArqExecutor(
        env.read_tasks,
        pool=pool,
        queue_name=queue,
        runners=registry,
        retry_backoff=0.01,
    )
    page = operation(env)
    put = env.read_tasks._put_context
    injected = False

    async def lose(task_id, values, *, guard):
        nonlocal injected
        cursor = values.get("revision_scan", {}).get("before")
        if cursor is not None and not injected:
            injected = True
            if after_commit:
                await put(task_id, values, guard=guard)
            raise ConnectionError("cursor acknowledgment lost")
        return await put(task_id, values, guard=guard)

    monkeypatch.setattr(env.read_tasks, "_put_context", lose)

    @registry.register(kind)
    async def run(ctx):
        try:
            report = await page.run_page(ctx)
        except ConnectionError as exc:
            raise RetriableError("scan cursor retry") from exc
        return Done(
            result={
                "cleaned": list(report.cleaned),
                "cycle_complete": report.cycle_complete,
            }
        )

    worker = _make_worker(pool, queue, executor, max_jobs=1)
    polling = None

    async def stop_polling() -> None:
        if polling is not None:
            await stop_workers([polling], [worker])

    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(pool.aclose)
        cleanup.callback(registry.unregister, kind)
        cleanup.push_async_callback(worker.close)
        cleanup.push_async_callback(pool.delete, queue)
        cleanup.push_async_callback(executor.shutdown, timeout=5)
        cleanup.push_async_callback(stop_polling)
        task = await env.tasks.create(kind, max_attempts=3)
        polling = asyncio.create_task(worker.async_run())
        await executor.submit(task.task_id)
        done = await wait_for_terminal(env.tasks, task.task_id, timeout=20)
        assert done.status is TaskStatus.SUCCEEDED and done.attempts == 2
        assert injected
        assert done.context["revision_scan"]["kind"] == "revision-publication-test"
        assert done.result["cleaned"] == ([] if after_commit else [env.ctx.task_id])
        assert (
            await env.reader.count(
                env.plan.key.kb_id,
                filter={"document_revision": env.plan.key.revision_id},
            )
            == 0
        )
        await _assert_visible(env.service, winner)


async def test_real_cancellation_vs_activation_race_preserves_published_version(
    publication_env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = publication_env
    await env.tasks.transition(env.ctx.task_id, TaskStatus.CANCELLING)
    await env.tasks.transition(env.ctx.task_id, TaskStatus.CANCELLED)
    abandon = env.read_documents.abandon_revision

    async def published_first(key):
        await env.documents.activate_revision(key)
        await abandon(key)

    delete = AsyncMock(wraps=env.reader.delete)
    monkeypatch.setattr(env.read_documents, "abandon_revision", published_first)
    monkeypatch.setattr(env.reader, "delete", delete)
    from comet_rag.services.revision_reconciliation import ReconciliationDisposition

    result = await operation(env).reconcile(
        await env.read_tasks.require(env.ctx.task_id)
    )
    assert result is ReconciliationDisposition.DEFERRED
    delete.assert_not_awaited()
    await _assert_visible(env.service, env.plan)
