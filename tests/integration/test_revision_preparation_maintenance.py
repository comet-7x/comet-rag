from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest
from arq.worker import create_worker

from comet_rag.composition.bootstrap import build_context
from comet_rag.config.schemas import APPConfig
from comet_rag.infrastructure.persistence.vector_store.milvus import AsyncMilvusStore
from comet_rag.infrastructure.task_execution.arq import LANE_QUEUES, ArqExecutor
from comet_rag.ports.document_store import RevisionStatus
from comet_rag.services.revision_context import (
    get_revision_key,
    load_preparation,
    load_task_handoff,
)
from comet_rag.services.revision_maintenance import RevisionMaintenance
from comet_rag.tasks import TaskContext, TaskStatus
from comet_rag.workers import base as workers
from comet_rag.workers.embedder import PROFILE
from tests.contracts.support import wait_until
from tests.integration.test_revision_lifecycle import _assert_visible, _Embedding, _plan
from tests.integration.test_revision_publication_recovery import Environment
from tests.integration.test_revision_publication_recovery import (
    publication_env as publication_env,
)
from tests.integration.test_revision_reconciliation_recovery import (
    finish_source,
    late_write,
    operation,
)
from tests.integration.worker_config import make_worker_config
from tests.integration.worker_lifecycle import stop_workers

pytestmark = pytest.mark.integration


async def preparing(env: Environment):
    task = await env.tasks.create(
        "revision-publication-test", context={"text": "新准备版本"}
    )
    await env.tasks.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="prepare", attempts=1
    )
    ctx = TaskContext(env.tasks, task.task_id, worker_id="prepare", attempts=1)
    key = await get_revision_key(ctx, kb_id=env.plan.key.kb_id, source_id="source")
    return ctx, _plan(key.revision_id, "量子纠缠新准备版本。" * 3)


async def controller(env: Environment, *, independent=False):
    service = RevisionMaintenance(
        task_store=env.read_tasks if independent else env.tasks,
        reconciler=operation(env),
        task_kind="revision-publication-test",
        resource_identity=env.prefix,
    )
    await service.initialize()
    return service


async def test_real_parent_commit_before_handoff_can_be_cancelled_and_recovered(
    publication_env: Environment, monkeypatch
):
    env = publication_env
    await env.stages().activate(env.ctx)
    ctx, plan = await preparing(env)
    put = env.documents.put_parents
    calls = env.model.calls

    async def lost(*args, **kwargs):
        await put(*args, **kwargs)
        raise ConnectionError("parents committed, handoff not written")

    with monkeypatch.context() as patch:
        patch.setattr(env.documents, "put_parents", lost)
        with pytest.raises(ConnectionError):
            await env.stages().prepare(ctx, plan)
    source = await env.read_tasks.require(ctx.task_id)
    draft = load_preparation(source.context, source.task_id)
    assert draft is not None and "revision_index" not in source.context
    before = await env.read_documents.reserve_revision(draft)
    assert await env.read_documents.get_parents(
        plan.key.kb_id, [p.id for p in plan.parents]
    )
    await env.tasks.transition(ctx.task_id, TaskStatus.CANCELLING)
    await env.tasks.transition(ctx.task_id, TaskStatus.CANCELLED)
    # 独立连接只凭持久化准备意图发现并清理，不调用原 prepare 或模型。
    monitor = await controller(env, independent=True)
    report = await monitor.run_once()
    assert report is not None and report.cleaned == (ctx.task_id,)
    after = await env.read_documents.reserve_revision(draft)
    assert (
        after.status is RevisionStatus.PURGED and after.generation == before.generation
    )
    assert (
        await env.read_documents.get_parents(
            plan.key.kb_id, [p.id for p in plan.parents]
        )
        == {}
    )
    assert (await env.read_tasks.require(ctx.task_id)).context["text"] == "新准备版本"
    assert env.model.calls == calls
    await _assert_visible(env.service, env.plan)


@pytest.mark.parametrize("committed", [False, True])
async def test_real_handoff_confirmation_loss_reuses_prepared_generation(
    publication_env: Environment, committed, monkeypatch
):
    env = publication_env
    ctx, plan = await preparing(env)
    put = ctx.put_bounded

    async def lost(limit, **values):
        if "revision_index" not in values:
            return await put(limit, **values)
        if committed:
            await put(limit, **values)
        raise ConnectionError("handoff confirmation lost")

    with monkeypatch.context() as patch:
        patch.setattr(ctx, "put_bounded", lost)
        with pytest.raises(ConnectionError):
            await env.stages().prepare(ctx, plan)
    fresh = TaskContext(env.read_tasks, ctx.task_id, worker_id="prepare", attempts=1)
    source = await fresh.snapshot()
    draft = (
        load_task_handoff(source.context, source.task_id).draft
        if committed
        else load_preparation(source.context, source.task_id)
    )
    assert draft is not None
    before = await env.read_documents.reserve_revision(draft)
    await env.stages(independent=True).prepare(fresh, None if committed else plan)
    source = await fresh.snapshot()
    assert source.context["text"] is None and source.context["revision_prepare"] is None
    assert (
        await env.read_documents.reserve_revision(draft)
    ).generation == before.generation


async def test_real_competing_controllers_only_one_owns_a_page(
    publication_env: Environment, monkeypatch
):
    env = publication_env
    winner = await finish_source(env)
    await late_write(env)
    first, second = await controller(env), await controller(env, independent=True)
    delete = env.reader.delete
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        await delete(*args, **kwargs)

    monkeypatch.setattr(env.reader, "delete", blocked)
    running = asyncio.create_task(first.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await asyncio.wait_for(second.run_once(), 5) is None
        assert (await first.status())["task_id"] == (await second.status())["task_id"]
    finally:
        release.set()
        await running
    assert (await second.status())["status"] == "pending"
    await _assert_visible(env.service, winner)


@pytest.mark.parametrize("failure", ["before-page", "after-page"])
async def test_real_worker_cron_recovers_with_new_context(
    publication_env: Environment,
    postgres_dsn,
    redis_url,
    milvus_uri,
    milvus_database,
    failure,
    monkeypatch,
):
    env = publication_env
    winner = await finish_source(env)
    await late_write(env)
    raw = make_worker_config(
        postgres_dsn,
        redis_url,
        redis_db=10,
        dim=2,
        milvus_uri=milvus_uri,
        milvus_database=milvus_database,
        collection_prefix=env.prefix,
    ).model_dump(mode="python")
    raw["revision_reconciliation"] = {
        "enabled": True,
        "task_kind": "revision-publication-test",
        "page_size": 1,
    }
    config = APPConfig.model_validate(raw)
    async with AsyncExitStack() as stack:
        producer = await build_context(config, embedding_model=_Embedding())
        assert isinstance(producer.vector_store, AsyncMilvusStore)
        assert producer.vector_store.consistency_level == "Session"
        stack.push_async_callback(producer.close)
        monitor = producer.revision_maintenance
        assert monitor is not None
        monitor_id = (await monitor.status())["task_id"]
        executor = producer.task_executor
        assert isinstance(executor, ArqExecutor)
        pool = await executor.pool()
        await pool.delete(*LANE_QUEUES.values())
        stack.push_async_callback(pool.delete, *LANE_QUEUES.values())

        async def start(*, inject):
            settings = workers.build_settings(
                PROFILE, config=config, embedding_model=_Embedding()
            )
            startup = settings.on_startup

            async def failing_startup(ctx):
                await startup(ctx)
                target = ctx["context"].revision_maintenance
                page = target._reconciler.run_page

                async def lost(scan):
                    if failure == "after-page":
                        await page(scan)
                    raise ConnectionError("scheduled page response lost")

                monkeypatch.setattr(target._reconciler, "run_page", lost)

            if inject:
                settings.on_startup = staticmethod(failing_startup)
            worker = create_worker(
                settings,
                poll_delay=0.01,
                handle_signals=False,
                keep_result=0,
                log_results=False,
            )
            polling = asyncio.create_task(worker.async_run())
            stopped = False

            async def stop():
                nonlocal stopped
                if not stopped:
                    await stop_workers([polling], [worker])
                    await worker.close()
                    stopped = True

            stack.push_async_callback(stop)
            return worker, polling, stop

        first, first_polling, stop_first = await start(inject=True)

        async def failed():
            if first_polling.done():
                first_polling.result()
            return (
                await env.read_tasks.require(monitor_id)
            ).status is TaskStatus.FAILED

        await wait_until(failed, timeout=15, message="cron 没有记录对账失败")
        saved = await env.read_tasks.require(monitor_id)
        assert (
            saved.error is not None
            and saved.error.code == "revision_reconciliation_failed"
        )
        await stop_first()
        second, second_polling, _ = await start(inject=False)

        async def resumed():
            if second_polling.done():
                second_polling.result()
            current = await env.read_tasks.require(monitor_id)
            return (
                current.status is TaskStatus.PENDING
                and current.attempts >= 2
                and current.result is not None
            )

        await wait_until(resumed, timeout=15, message="新 worker 未从保留游标恢复")
        assert (await second.ctx["context"].revision_maintenance.status())[
            "task_id"
        ] == monitor_id
        condition = {
            "source_id": "source",
            "document_revision": env.plan.key.revision_id,
        }
        assert await env.reader.count(env.plan.key.kb_id, filter=condition) == 0
        await _assert_visible(env.service, winner)


async def test_real_maintenance_cursor_does_not_exhaust_business_admission(
    publication_env: Environment, postgres_dsn, redis_url, milvus_uri, milvus_database
):
    from comet_rag.tasks import Done
    from comet_rag.tasks.service import Backlogged

    env = publication_env
    raw = make_worker_config(
        postgres_dsn,
        redis_url,
        redis_db=10,
        dim=2,
        milvus_uri=milvus_uri,
        milvus_database=milvus_database,
        collection_prefix=env.prefix,
    ).model_dump(mode="python")
    raw["revision_reconciliation"] = {
        "enabled": True,
        "task_kind": "revision-publication-test",
    }
    raw["limits"]["max_backlog"] = 1
    async with AsyncExitStack() as stack:
        context = await build_context(
            APPConfig.model_validate(raw), embedding_model=_Embedding()
        )
        stack.push_async_callback(context.close)
        executor = context.task_executor
        assert isinstance(executor, ArqExecutor)
        pool = await executor.pool()
        await pool.delete(*LANE_QUEUES.values())
        stack.push_async_callback(pool.delete, *LANE_QUEUES.values())

        async def probe(ctx):
            return Done()

        context.runners.register("admission-probe")(probe)
        assert (await context.task_service.backlog())["pending"] == 0
        task = await context.task_service.submit("admission-probe")
        assert (await env.read_tasks.require(task.task_id)).status is TaskStatus.PENDING
        assert (await context.task_service.backlog())["pending"] == 1
        with pytest.raises(Backlogged):
            await context.task_service.submit("admission-probe")
