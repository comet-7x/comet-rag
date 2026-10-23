from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from arq import ArqRedis

from comet_rag.composition import Context, bootstrap
from comet_rag.config.schemas import Backend, RedisConfig
from comet_rag.infrastructure.task_execution.arq import ArqExecutor
from comet_rag.services.ingestion import INGEST_KIND, IngestRunner
from comet_rag.tasks import (
    Done,
    InMemoryTaskStore,
    InProcessExecutor,
    RunnerRegistry,
    StagePipeline,
    TaskContext,
    TaskExecutor,
    TaskStatus,
)
from comet_rag.tasks.runner import UnknownKind
from tests.unit.tasks.test_delivery_recovery import FakeQueue
from tests.unit.test_bootstrap import FakeEmbedding, make_config


class OwnedEmbedding(FakeEmbedding):
    def __init__(self, owner: str) -> None:
        super().__init__()
        self.owner = owner


@pytest.fixture
async def contexts() -> AsyncIterator[list[Context]]:
    contexts: list[Context] = []
    try:
        yield contexts
    finally:
        for context in reversed(contexts):
            await context.close()


async def build(contexts: list[Context]) -> Context:
    context = await bootstrap.build_context(
        make_config(),
        embedding_model=OwnedEmbedding("first" if not contexts else "second"),
    )
    contexts.append(context)
    return context


async def identify_owner(self: IngestRunner, ctx: TaskContext) -> Done:
    # 只替换业务计算；runner 查找、任务认领和终态写入仍走实际执行器。
    model = self._embedding_model  # noqa: SLF001 — 用实际 runner 依赖标识所有者
    assert isinstance(model, OwnedEmbedding)
    return Done(result=model.owner)


async def assert_execution(context: Context, owner: str) -> None:
    assert isinstance(context.task_executor, InProcessExecutor)
    task = await context.task_store.create(INGEST_KIND)
    await context.task_executor.execute(task.task_id)
    done = await context.task_store.require(task.task_id)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.result == owner


@pytest.mark.parametrize("close_first", [0, 1])
async def test_contexts_execute_own_runner_before_and_after_peer_closes(
    contexts: list[Context], monkeypatch: pytest.MonkeyPatch, close_first: int
) -> None:
    monkeypatch.setattr(IngestRunner, "__call__", identify_owner)
    first, second = await build(contexts), await build(contexts)
    for context, name in [(first, "first"), (second, "second")]:
        assert isinstance(context.task_executor, InProcessExecutor)
        assert context.task_executor.runners is context.runners
        await assert_execution(context, name)
    assert first.runners is not second.runners
    await contexts[close_first].close()
    with pytest.raises(UnknownKind):
        contexts[close_first].runners.get(INGEST_KIND)
    survivor = contexts[1 - close_first]
    await assert_execution(survivor, "second" if close_first == 0 else "first")


@pytest.mark.parametrize(
    "failure", [RuntimeError("late startup failure"), asyncio.CancelledError()]
)
async def test_failed_peer_startup_revokes_only_its_own_registration(
    contexts: list[Context], monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    first = await build(contexts)
    original = first.runners.get(INGEST_KIND)
    monkeypatch.setattr(IngestRunner, "__call__", identify_owner)
    failed: list[Context] = []
    wire = bootstrap.wire_runners

    def fail_after_registration(context: Context, *, ingest_config: object) -> None:
        wire(context, ingest_config=ingest_config)
        failed.append(context)
        raise failure

    monkeypatch.setattr(bootstrap, "wire_runners", fail_after_registration)
    with pytest.raises(type(failure)) as caught:
        await build(contexts)
    assert caught.value is failure
    assert first.runners.get(INGEST_KIND) is original
    assert failed[0].runners.kinds() == []
    await assert_execution(first, "first")


async def test_registration_survives_executor_drain_and_is_revoked_before_resource_close(
    contexts: list[Context], monkeypatch: pytest.MonkeyPatch
) -> None:
    context = await build(contexts)
    runner = context.runners.get(INGEST_KIND)
    draining, release = asyncio.Event(), asyncio.Event()

    async def shutdown(*, timeout: float | None = 10.0) -> None:
        assert context.runners.get(INGEST_KIND) is runner
        draining.set()
        await release.wait()
        assert context.runners.get(INGEST_KIND) is runner

    registrations_at_model_close: list[list[str]] = []

    async def close_model() -> None:
        registrations_at_model_close.append(context.runners.kinds())

    monkeypatch.setattr(context.task_executor, "shutdown", shutdown)
    monkeypatch.setattr(context.embedding_model, "close", close_model)
    closing = asyncio.create_task(context.close())
    try:
        await asyncio.wait_for(draining.wait(), 1)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert context.runners.get(INGEST_KIND) is runner
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
    assert context.runners.kinds() == []
    assert registrations_at_model_close == [[]]


@pytest.mark.parametrize("kind", ["ingest", "other-job"])
@pytest.mark.parametrize("backend", ["inprocess", "arq"])
async def test_executor_defaults_have_no_shared_or_fallback_bindings(
    kind: str, backend: str
) -> None:
    first_store, second_store = InMemoryTaskStore(), InMemoryTaskStore()
    if backend == "inprocess":
        first, second = InProcessExecutor(first_store), InProcessExecutor(second_store)
    else:
        first, second = ArqExecutor(first_store), ArqExecutor(second_store)

    @first.runners.register(kind)
    async def runner(ctx: TaskContext) -> Done:
        return Done(result="first")

    try:
        first_task = await first_store.create(kind)
        second_task = await second_store.create(kind)
        await first.execute(first_task.task_id)
        await second.execute(second_task.task_id)
        assert (await first_store.require(first_task.task_id)).result == "first"
        failed = await second_store.require(second_task.task_id)
        assert failed.status is TaskStatus.FAILED
        assert failed.error is not None
        assert "未注册" in failed.error.message
        assert second.runners.kinds() == []
    finally:
        await first.shutdown()
        await second.shutdown()


async def test_arq_routes_each_resume_stage_using_its_own_registry() -> None:
    executors: list[ArqExecutor] = []
    try:
        for lane in ("cpu", "io"):
            store, queue = InMemoryTaskStore(), FakeQueue()
            executor = ArqExecutor(
                store,
                pool=cast(ArqRedis, queue),
                lanes={"cpu": "cpu-queue", "io": "io-queue"},
                entry_lane="cpu",
            )
            executors.append(executor)
            flow = StagePipeline()

            @flow.stage("resume", lane=lane)
            async def stage(ctx: TaskContext) -> Done:
                return Done(result="ok")

            executor.runners.register("same-kind")(flow)
        # 两者全部绑定完后才投递，才能揭露“最后注册者覆盖所有应用”的缺陷。
        for executor, expected in zip(
            executors, ("cpu-queue", "io-queue"), strict=True
        ):
            task = await executor._store.create("same-kind", resume_stage="resume")  # noqa: SLF001
            await executor.submit(task.task_id)
            queue = cast(FakeQueue, executor._pool)  # noqa: SLF001
            assert [item[1] for item in queue.jobs.values()] == [expected]
    finally:
        for executor in executors:
            await executor.shutdown()


def test_registry_replacement_and_identity_cleanup_are_local() -> None:
    first, second = RunnerRegistry(), RunnerRegistry()

    @first.register("kind")
    async def old(ctx: TaskContext) -> Done:
        return Done(result="old")

    @first.register("kind", replace=True)
    async def new(ctx: TaskContext) -> Done:
        return Done(result="new")

    first.unregister("kind", expected=old)
    assert first.get("kind") is new
    assert second.kinds() == []
    first.unregister("kind", expected=new)
    assert first.kinds() == []


async def test_context_close_releases_explicit_runner_replacements(
    contexts: list[Context],
) -> None:
    context = await build(contexts)

    @context.runners.register(INGEST_KIND, replace=True)
    async def replacement(ctx: TaskContext) -> Done:
        return Done(result="replaced")

    @context.runners.register("custom")
    async def custom(ctx: TaskContext) -> Done:
        return Done(result="custom")

    await context.close()
    assert context.runners.kinds() == []


async def test_explicit_registry_is_shared_with_built_executor() -> None:
    runners = RunnerRegistry()
    context = await bootstrap.build_context(
        make_config(), embedding_model=FakeEmbedding(), runners=runners
    )
    try:
        assert context.runners is runners
        assert isinstance(context.task_executor, InProcessExecutor)
        assert context.task_executor.runners is runners
        assert runners.kinds() == [INGEST_KIND]
    finally:
        await context.close()
    assert runners.kinds() == []


async def test_mismatched_executor_registry_is_rejected_before_resource_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = InProcessExecutor(InMemoryTaskStore())

    def unexpected_creation(config: object) -> None:
        pytest.fail("注册表不匹配时不应创建模型")

    monkeypatch.setattr(bootstrap, "build_embedding_model", unexpected_creation)
    try:
        with pytest.raises(ValueError, match="同一实例"):
            await bootstrap.build_context(
                make_config(), task_executor=executor, runners=RunnerRegistry()
            )
        assert executor.runners.kinds() == []
    finally:
        await executor.shutdown()


class QueueOnlyExecutor(TaskExecutor):
    async def submit(self, task_id: str, *, delay: float = 0.0) -> None:
        return None

    async def request_cancel(self, task_id: str) -> bool:
        return False

    async def shutdown(self, *, timeout: float | None = 10.0) -> None:
        return None


async def test_custom_executor_requires_explicit_consumer_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = QueueOnlyExecutor()

    def unexpected_creation(config: object) -> None:
        pytest.fail("缺少注册表时不应创建模型")

    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "build_embedding_model", unexpected_creation)
        with pytest.raises(ValueError, match="必须同时提供 runners"):
            await bootstrap.build_context(make_config(), task_executor=executor)
    runners = RunnerRegistry()
    context = await bootstrap.build_context(
        make_config(),
        task_executor=executor,
        runners=runners,
        embedding_model=FakeEmbedding(),
    )
    try:
        assert context.runners is runners
        assert runners.kinds() == [INGEST_KIND]
    finally:
        await context.close()
    assert runners.kinds() == []


async def test_reused_executor_startup_failure_leaves_live_owner_executable(
    contexts: list[Context],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await build(contexts)
    original = first.runners.get(INGEST_KIND)
    monkeypatch.setattr(IngestRunner, "__call__", identify_owner)
    with pytest.raises(ValueError, match="重复注册"):
        await bootstrap.build_context(
            make_config(),
            embedding_model=FakeEmbedding(),
            task_store=first.task_store,
            task_executor=first.task_executor,
        )
    assert first.runners.get(INGEST_KIND) is original
    await assert_execution(first, "first")


async def test_failed_startup_preserves_preexisting_injected_registry_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runners = RunnerRegistry()

    @runners.register("preexisting")
    async def existing(ctx: TaskContext) -> Done:
        return Done(result="caller-owned")

    wire = bootstrap.wire_runners
    error = RuntimeError("after registration")

    def fail_after_registration(context: Context, *, ingest_config: object) -> None:
        wire(context, ingest_config=ingest_config)
        raise error

    monkeypatch.setattr(bootstrap, "wire_runners", fail_after_registration)
    with pytest.raises(RuntimeError) as caught:
        await bootstrap.build_context(
            make_config(), embedding_model=FakeEmbedding(), runners=runners
        )
    assert caught.value is error
    assert runners.kinds() == ["preexisting"]
    assert runners.get("preexisting") is existing


async def test_arq_factory_passes_explicit_registry_to_executor() -> None:
    runners = RunnerRegistry()
    config = make_config(
        task_executor=Backend.ARQ,
        task_store=Backend.POSTGRES,
        vector_store=Backend.MILVUS,
    )
    config.infrastructure_config.redis = RedisConfig(host="localhost", port=6379)
    store = InMemoryTaskStore()
    executor = bootstrap.build_task_executor(config, store, runners=runners)
    assert isinstance(executor, ArqExecutor)
    try:
        assert executor.runners is runners

        @runners.register("explicit")
        async def runner(ctx: TaskContext) -> Done:
            return Done(result="explicit")

        task = await store.create("explicit")
        await executor.execute(task.task_id)
        done = await store.require(task.task_id)
        assert done.status is TaskStatus.SUCCEEDED
        assert done.result == "explicit"
    finally:
        await executor.shutdown()
