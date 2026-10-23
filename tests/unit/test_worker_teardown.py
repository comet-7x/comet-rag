from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from arq.worker import Worker

from tests.integration.test_e2e_workers import _running
from tests.integration.test_runner_registry_workers import Deployment, stop_polling


@pytest.mark.parametrize("entry", ["e2e", "registry"])
@pytest.mark.parametrize("transport_fails", [False, True])
async def test_worker_teardown_finishes_transport_ack_before_sdk_pool_close(
    entry,
    transport_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, leave = asyncio.Event(), asyncio.Event()
    acknowledging, release_ack, acknowledged = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    failure = RuntimeError("transport acknowledgement failed")

    async def job(ctx) -> None:
        return None

    async def poll() -> None:
        await asyncio.Event().wait()

    async def finish_job() -> None:
        acknowledging.set()
        await release_ack.wait()
        acknowledged.set()
        if transport_fails:
            raise failure

    # 只替换网络池与轮询；close 保留 ARQ 的真实实现，验证 cancelled task 会阻断关闭的边界。
    worker = Worker([job], handle_signals=False)
    pool = AsyncMock()
    worker._pool = pool  # noqa: SLF001 — 不创建真实 Redis 连接
    monkeypatch.setattr(worker, "async_run", AsyncMock(side_effect=poll))
    transport = asyncio.create_task(finish_job())
    worker.tasks["finished-business-job"] = transport
    assert not worker.job_tasks

    async def scope() -> None:
        if entry == "e2e":
            async with _running(worker):
                entered.set()
                await leave.wait()
        else:
            polling = asyncio.create_task(worker.async_run())
            # 只使用 stop_polling 所需的两个观测面，不绕过真实 SDK close。
            deployment = SimpleNamespace(polling=[polling], workers=[worker])
            entered.set()
            await leave.wait()
            try:
                await stop_polling(cast("Deployment", deployment))
            finally:
                await worker.close()

    closing = asyncio.create_task(scope())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(acknowledging.wait(), 1)
        leave.set()
        _, pending = await asyncio.wait({closing}, timeout=0.01)
        assert closing in pending, "夹具取消了仍在确认结果的 run_job"
        assert not transport.cancelled()
        pool.close.assert_not_awaited()
    finally:
        release_ack.set()
        outcome = (await asyncio.gather(closing, return_exceptions=True))[0]
        await asyncio.gather(transport, return_exceptions=True)

    if transport_fails:
        assert isinstance(outcome, BaseExceptionGroup)
        assert outcome.exceptions == (failure,)
    else:
        assert outcome is None
    assert acknowledged.is_set()
    pool.delete.assert_awaited_once()
    pool.close.assert_awaited_once_with(close_connection_pool=True)


async def test_teardown_cancels_business_job_and_preserves_its_transport_wrapper() -> (
    None
):
    from tests.integration.worker_lifecycle import stop_workers

    entered = asyncio.Event()

    async def business(ctx) -> None:
        entered.set()
        await asyncio.Event().wait()

    worker = Worker([business], handle_signals=False)
    pool = AsyncMock()
    worker._pool = pool  # noqa: SLF001 — 网络边界替身，关闭仍使用原生 SDK
    job = asyncio.create_task(business({}))

    async def transport() -> None:
        try:
            await job
        except asyncio.CancelledError:
            # 与 SDK 一样，业务取消仍需由包装任务完成传输收尾。
            return None

    wrapper = asyncio.create_task(transport())
    worker.job_tasks["job"] = job
    worker.tasks["job"] = wrapper
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(stop_workers([], [worker]), 0.1)
        assert job.cancelled()
        assert wrapper.done() and not wrapper.cancelled()
        assert not worker.tasks
    finally:
        job.cancel()
        await asyncio.gather(job, wrapper, return_exceptions=True)
        worker.tasks.clear()
        await worker.close()
    pool.close.assert_awaited_once_with(close_connection_pool=True)
