from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from comet_rag._async import map_concurrently, run_sync


def test_cancel_waits_for_blocking_reader_even_after_repeated_cancellation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "input"
    path.write_text("retained")
    started = threading.Event()
    release = threading.Event()
    read: list[str] = []

    def reader() -> None:
        started.set()
        assert release.wait(5)
        read.append(path.read_text())

    async def scenario() -> None:
        task = asyncio.create_task(run_sync(reader))
        await asyncio.to_thread(started.wait, 5)
        try:
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert path.exists()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert read == ["retained"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_failed_batch_stops_dispatch_and_drains_cancel_suppressing_sibling() -> None:
    async def scenario() -> None:
        started: list[str] = []
        held = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        failure = OSError("upstream")

        async def operation(item: str) -> str:
            started.append(item)
            if item == "fail":
                await held.wait()
                raise failure
            held.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
            return item

        task = asyncio.create_task(
            map_concurrently(operation, ["fail", "held", "queued"], limit=2)
        )
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            with pytest.raises(OSError) as caught:
                await task
            assert caught.value is failure
            assert started == ["fail", "held"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_batch_is_bounded_and_preserves_order() -> None:
    async def scenario() -> None:
        active = peak = 0

        async def operation(item: int) -> int:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return item * 10

        assert await map_concurrently(operation, list(range(20)), limit=2) == list(
            range(0, 200, 10)
        )
        assert peak == 2
        for limit in (0, -1, True):
            with pytest.raises(ValueError):
                await map_concurrently(operation, [1], limit=limit)

    asyncio.run(scenario())
