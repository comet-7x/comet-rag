"""真实 Redis/PostgreSQL 上验证注册所有权；向量与模型不参与本组任务。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

import pytest
from arq.jobs import Job
from arq.worker import Worker, create_worker
from sqlalchemy import text

from comet_rag.composition import Context, bootstrap
from comet_rag.config.schemas import APPConfig
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.task_store.postgres import PostgresTaskStore
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.infrastructure.task_execution.arq import LANE_QUEUES, ArqExecutor
from comet_rag.tasks import Done, RetriableError, StagePipeline, TaskContext, TaskStatus
from comet_rag.workers import base as workers
from comet_rag.workers.embedder import PROFILE as EMBEDDER
from comet_rag.workers.maintenance import sweep_stale_tasks
from comet_rag.workers.preprocessor import PROFILE as PREPROCESSOR
from tests.contracts.support import wait_for_terminal, wait_until
from tests.integration.conftest import truncate_tables
from tests.integration.worker_config import make_worker_config

pytestmark = pytest.mark.integration
KIND = "registry-probe"


class OwnedEmbedding(AsyncBaseEmbeddingModel):
    def __init__(self, owner: str) -> None:
        self.owner = owner

    # Any：沿用模型 Port 的供应商扩展参数；本组任务不调用模型。
    async def _embed(self, data: str, **kwargs: Any) -> list[float]:
        pytest.fail("registry-probe 不应调用模型")


def bind_probe(context: Context) -> None:
    model = context.embedding_model
    assert isinstance(model, OwnedEmbedding)
    flow = StagePipeline()

    @flow.stage("prepare", lane="cpu")
    async def prepare(ctx: TaskContext) -> None:
        await ctx.put(trace=[model.owner], prepared=True)

    @flow.stage("index", lane="io")
    async def index(ctx: TaskContext) -> Done:
        snapshot = await ctx.snapshot()
        assert snapshot.context["prepared"] is True
        if snapshot.request.get("retry") and snapshot.attempts == 1:
            raise RetriableError("注入 IO 阶段第一次失败")
        return Done(result=[*snapshot.context["trace"], model.owner])

    context.runners.register(KIND)(flow)


@dataclass
class Deployment:
    config: APPConfig
    producer: Context
    workers: list[Worker]
    stack: AsyncExitStack
    polling: list[asyncio.Task[None]] = field(default_factory=list)
    contexts: list[Context] = field(default_factory=list)

    async def start(self, worker: Worker) -> None:
        polling = asyncio.create_task(worker.async_run())
        self.polling.append(polling)

        async def ready() -> bool:
            if polling.done():
                polling.result()  # 启动失败应传播原错误，不能等成一个无关的超时。
            return "context" in worker.ctx

        await wait_until(ready, timeout=10, message="worker 启动未发布 Context")
        context = worker.ctx["context"]
        self.contexts.append(context)
        bind_probe(context)

    async def close(self) -> None:
        await self.stack.aclose()

    async def submit(self, *, retry: bool = False) -> str:
        task = await self.producer.task_service.submit(
            KIND, {"retry": retry}, max_attempts=3
        )
        return task.task_id


async def stop_polling(deployment: Deployment) -> None:
    for polling in deployment.polling:
        polling.cancel()
    await asyncio.gather(*deployment.polling, return_exceptions=True)
    # 所有生产者先停，再排干 job，最后由退出栈关闭 Context 和连接池。
    jobs = [job for worker in deployment.workers for job in worker.tasks.values()]
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)


@pytest.fixture
async def deployments(
    postgres_dsn: str,
    redis_url: str,
) -> AsyncIterator[Callable[..., Awaitable[Deployment]]]:
    await truncate_tables(postgres_dsn, "tasks", "knowledge_bases")
    created: list[Deployment] = []

    async def create(owner: str, *, start_io: bool = True) -> Deployment:
        # 用例独占测试 Redis 的 11/12 DB；应用间不共享队列，更不声称多租户隔离。
        config = make_worker_config(postgres_dsn, redis_url, redis_db=11 + len(created))
        stack = AsyncExitStack()
        producer = await bootstrap.build_context(
            config,
            embedding_model=OwnedEmbedding(f"{owner}/api"),
            vector_store=AsyncInMemoryVectorStore(),
        )
        stack.push_async_callback(producer.close)
        instance = Deployment(config, producer, [], stack, contexts=[producer])
        created.append(instance)
        bind_probe(producer)
        for profile in (PREPROCESSOR, EMBEDDER):
            settings = workers.build_settings(
                replace(profile, sweep=False),
                config=config,
                embedding_model=OwnedEmbedding(f"{owner}/{profile.lane}"),
                vector_store=AsyncInMemoryVectorStore(),
            )
            worker = create_worker(
                settings,
                poll_delay=0.01,
                handle_signals=False,
                keep_result=1,
                log_results=False,
            )
            instance.workers.append(worker)
            stack.push_async_callback(worker.close)
        stack.push_async_callback(stop_polling, instance)
        # 只删本测试专用 DB 下固定的生产队列，不影响开发 db=0。
        executor = producer.task_executor
        assert isinstance(executor, ArqExecutor)
        pool = await executor.pool()
        await pool.delete(*LANE_QUEUES.values())
        await instance.start(instance.workers[0])
        if start_io:
            await instance.start(instance.workers[1])
        return instance

    try:
        yield create
    finally:
        for instance in reversed(created):
            await instance.close()
        await truncate_tables(postgres_dsn, "tasks", "knowledge_bases")


@pytest.mark.parametrize("close_first", [0, 1])
async def test_multiple_contexts_keep_own_runners_across_peer_shutdown(
    deployments: Callable[..., Awaitable[Deployment]],
    close_first: int,
) -> None:
    first, second = await deployments("A"), await deployments("B")
    applications = [first, second]
    contexts = [context for app in applications for context in app.contexts]
    assert len({id(context.runners) for context in contexts}) == 6
    assert len({id(context.database.engine) for context in contexts}) == 6
    ids = [await app.submit() for app in applications]
    for app, task_id, expected in zip(
        applications,
        ids,
        (["A/cpu", "A/io"], ["B/cpu", "B/io"]),
        strict=True,
    ):
        done = await wait_for_terminal(app.producer.task_store, task_id, timeout=10)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.result == expected
    await applications[close_first].close()
    assert all(
        not context.runners.kinds() for context in applications[close_first].contexts
    )
    survivor = applications[1 - close_first]
    task_id = await survivor.submit()
    done = await wait_for_terminal(survivor.producer.task_store, task_id, timeout=10)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.result == (["B/cpu", "B/io"] if close_first == 0 else ["A/cpu", "A/io"])


@pytest.mark.parametrize("retry", [False, True])
async def test_handoff_resumes_using_only_io_context_after_cpu_context_closes(
    deployments: Callable[..., Awaitable[Deployment]],
    retry: bool,
) -> None:
    app = await deployments("A", start_io=False)
    task_id = await app.submit(retry=retry)

    executor = app.producer.task_executor
    assert isinstance(executor, ArqExecutor)
    pool = await executor.pool()

    async def handed_off() -> bool:
        snapshot = await app.producer.task_store.require(task_id)
        # PostgreSQL 的 PENDING 先落库，Redis 投递随后完成；不能把前者当作跨库提交。
        return (
            snapshot.status is TaskStatus.PENDING
            and snapshot.resume_stage == "index"
            and await pool.zcard(LANE_QUEUES["io"]) == 1
        )

    await wait_until(handed_off, timeout=10, message="CPU 未完成移交")
    handed = await app.producer.task_store.require(task_id)
    assert handed.attempts == 0
    jobs = await pool.zrange(LANE_QUEUES["io"], 0, -1)
    assert len(jobs) == 1
    info = await Job(jobs[0].decode(), redis=pool, _queue_name=LANE_QUEUES["io"]).info()
    assert info is not None and info.args == (task_id,) and info.kwargs == {}
    # CPU 的本地注册表及连接已关闭，IO 必须靠自己的依赖与持久化上下文续跑。
    app.polling[0].cancel()
    await asyncio.gather(app.polling[0], return_exceptions=True)
    await app.workers[0].close()
    assert app.contexts[1].runners.kinds() == []
    await app.start(app.workers[1])
    done = await wait_for_terminal(app.producer.task_store, task_id, timeout=10)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.result == ["A/cpu", "A/io"]
    assert done.attempts == (2 if retry else 1)
    assert [record.stage for record in done.stage_history].count("prepare") == 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_worker_startup_releases_real_connections_and_preserves_live_app(
    deployments: Callable[..., Awaitable[Deployment]],
    postgres_dsn: str,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    live = await deployments("A")
    config = make_worker_config(postgres_dsn, redis_url, redis_db=13)
    owned = Database(postgres_dsn)
    redis_settings = config.infrastructure_config.redis
    assert redis_settings is not None
    executor = ArqExecutor(PostgresTaskStore(owned), redis_url=redis_settings.url)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(owned.close)
        cleanup.push_async_callback(executor.shutdown)
        observer = live.producer.database
        assert observer is not None
        owner = f"failed-{uuid4().hex}"
        async with owned.transaction() as session:
            await session.execute(
                text("SELECT set_config('application_name', :name, false)"),
                {"name": owner},
            )
        redis = await executor.pool()
        client_id = await redis.client_id()

        @executor.runners.register("preexisting")
        async def existing(ctx: TaskContext) -> Done:
            return Done(result="original")

        failure = (
            asyncio.CancelledError()
            if cancelled
            else RuntimeError("after registration")
        )
        wire = bootstrap.wire_runners
        failed: list[Context] = []

        def fail_after_registration(context: Context, *, ingest_config: object) -> None:
            wire(context, ingest_config=ingest_config)
            failed.append(context)
            raise failure

        ctx: dict[str, Any] = {  # ARQ ctx 同时保存 profile、配置和装配参数。
            "profile": PREPROCESSOR,
            "config": config,
            "build_kwargs": {
                "embedding_model": OwnedEmbedding("B/cpu"),
                "vector_store": AsyncInMemoryVectorStore(),
            },
        }
        live_executor = live.producer.task_executor
        assert isinstance(live_executor, ArqExecutor)
        live_redis = await live_executor.pool()
        async with observer.session() as session:
            before = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name"
                    ),
                    {"name": owner},
                )
            ).scalar_one()
        assert before == 1
        assert any(
            int(client["id"]) == client_id for client in await live_redis.client_list()
        )
        with monkeypatch.context() as patch:
            patch.setattr(bootstrap, "build_database", lambda config: owned)
            patch.setattr(
                bootstrap, "build_task_executor", lambda *args, **kwargs: executor
            )
            patch.setattr(bootstrap, "wire_runners", fail_after_registration)
            with pytest.raises(type(failure)) as caught:
                await workers.on_startup(ctx)
        assert caught.value is failure
        assert "context" not in ctx and "executor" not in ctx
        assert failed[0].runners.kinds() == ["preexisting"]
        assert failed[0].runners.get("preexisting") is existing

        async def connections_released() -> bool:
            async with observer.session() as session:
                remaining = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name"
                        ),
                        {"name": owner},
                    )
                ).scalar_one()
            clients = await live_redis.client_list()
            return remaining == 0 and all(
                int(client["id"]) != client_id for client in clients
            )

        # Redis CLIENT LIST 覆盖同实例所有 DB；PostgreSQL 从独立连接观察，避免只检查 close 调用次数。
        await wait_until(
            connections_released, timeout=5, message="失败启动遗留真实连接"
        )
        task_id = await live.submit()
        done = await wait_for_terminal(live.producer.task_store, task_id, timeout=10)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.result == ["A/cpu", "A/io"]


async def test_handoff_enqueue_failure_is_repaired_with_own_resume_routing(
    deployments: Callable[..., Awaitable[Deployment]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = await deployments("A", start_io=False)
    cpu = app.workers[0].ctx["context"]
    executor = cpu.task_executor
    assert isinstance(executor, ArqExecutor)
    pool = await executor.pool()
    enqueue = pool.enqueue_job
    failed = asyncio.Event()

    # Any：透传 ARQ enqueue_job 的异构参数与返回值，仅注入首次投递故障。
    async def fail_first_io_enqueue(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("_queue_name") == LANE_QUEUES["io"] and not failed.is_set():
            failed.set()
            raise ConnectionError("注入移交投递失败")
        return await enqueue(*args, **kwargs)

    monkeypatch.setattr(pool, "enqueue_job", fail_first_io_enqueue)
    task_id = await app.submit()
    await asyncio.wait_for(failed.wait(), 10)
    # 停掉消费者再检查补投队列，避免错误路由被 CPU 再次移交而掩盖。
    await stop_polling(app)
    pending = await app.producer.task_store.require(task_id)
    assert pending.status is TaskStatus.PENDING
    assert pending.resume_stage == "index" and pending.attempts == 0
    assert await pool.zcard(LANE_QUEUES["io"]) == 0
    assert await sweep_stale_tasks({"context": cpu, "executor": executor}) == 1
    assert await pool.zcard(LANE_QUEUES["io"]) == 1
    await app.start(app.workers[1])
    done = await wait_for_terminal(app.producer.task_store, task_id, timeout=10)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.result == ["A/cpu", "A/io"]
    assert done.attempts == 1
    assert [record.stage for record in done.stage_history].count("prepare") == 1
