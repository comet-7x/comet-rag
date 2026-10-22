from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from comet_rag.engines.concurrency import map_concurrently
from comet_rag.engines.embedding import async_batch
from comet_rag.engines.pipelines.hooks import AsyncHookRegistry
from comet_rag.engines.pipelines.types import PipelineConfig
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.sources.base import AsyncBaseLoader
from comet_rag.ports import ExtractedDocument
from comet_rag.ports.source import LoadedResource, SourceContent
from comet_rag.services.pipeline import AsyncPipeline


class _BatchProbe:
    """一个触发失败的请求与一个需要异步收尾的兄弟请求。"""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.started = asyncio.Event()
        self.fail = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned = False
        self.cleanup_interrupted = False
        self.tasks: set[asyncio.Task[Any]] = set()
        self.released_files: list[str] = []

    async def run(self, label: str) -> None:
        current = asyncio.current_task()
        assert current is not None
        self.tasks.add(current)
        if label == "fail":
            await self.started.wait()
            await self.fail.wait()
            raise self.failure
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cleanup_interrupted = True
                raise
            self.cleaned = True

    async def dispose(self, batch: asyncio.Task[object]) -> None:
        self.release.set()
        batch.cancel()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(batch, *self.tasks, return_exceptions=True)


class _Embedding(AsyncBaseEmbeddingModel):
    def __init__(self, probe: _BatchProbe) -> None:
        self.probe = probe

    async def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        await self.probe.run(data)
        return [1.0]


class _Loader(AsyncBaseLoader):
    def __init__(self, probe: _BatchProbe, path: Path) -> None:
        self.probe = probe
        self.path = path

    async def _load(self, source: SourceContent | str, **kwargs: Any) -> LoadedResource:
        normalized = SourceContent(source) if isinstance(source, str) else source
        return LoadedResource(
            path=self.path,
            source=normalized,
            metadata={"file_type": "probe"},
            _release=lambda: self.probe.released_files.append(normalized.source),
        )

    async def cleanup(self) -> None:
        return None


async def _run_batch(kind: str, probe: _BatchProbe, path: Path) -> object:
    if kind == "embedding":
        return await async_batch.embed_documents(
            _Embedding(probe), ["fail", "held"], max_concurrency=2
        )
    hooks = AsyncHookRegistry()

    @hooks.extractor("probe")
    async def extract(
        content: LoadedResource, config: PipelineConfig
    ) -> ExtractedDocument:
        await probe.run(content.source.source)
        return ExtractedDocument(markdown="unused")

    pipeline = AsyncPipeline(
        PipelineConfig(max_concurrency=2), loader=_Loader(probe, path), hooks=hooks
    )
    return await pipeline.batch_run(["fail", "held"])


async def _checkpoint() -> None:
    """让已发出的取消进入等待者；不以经过多少时间判断调度状态。"""
    loop = asyncio.get_running_loop()
    resumed = loop.create_future()
    loop.call_soon(resumed.set_result, None)
    await resumed


@pytest.mark.parametrize("kind", ["embedding", "pipeline"])
@pytest.mark.parametrize("cause", ["error", "child_cancel", "request_cancel"])
async def test_batch_drains_siblings_before_propagating_failure(
    kind: str, cause: str, tmp_path: Path
) -> None:
    failure = (
        RuntimeError("upstream failure")
        if cause == "error"
        else asyncio.CancelledError("upstream cancelled")
    )
    probe = _BatchProbe(failure)
    batch = asyncio.create_task(_run_batch(kind, probe, tmp_path / "source"))
    try:
        await asyncio.wait_for(probe.started.wait(), 1)
        if cause == "request_cancel":
            batch.cancel("request cancelled")
        else:
            probe.fail.set()
        await asyncio.wait_for(probe.cleaning.wait(), 1)
        assert not batch.done(), "兄弟请求仍在清理时，批量调用不得先返回"

        # 后续取消只能影响最终传播，不能再打断已经开始的兄弟清理。
        for _ in range(2):
            batch.cancel("cancel again")
            await _checkpoint()
            assert not probe.cleanup_interrupted
            assert not batch.done()

        probe.release.set()
        with pytest.raises(type(failure)) as caught:
            await asyncio.wait_for(asyncio.shield(batch), 1)
        if cause == "error":
            assert caught.value is failure
        assert probe.cleaned
        if kind == "pipeline":
            assert sorted(probe.released_files) == ["fail", "held"]
    finally:
        await probe.dispose(batch)


async def test_concurrent_map_bounds_tasks_and_preserves_input_order() -> None:
    first_wave = asyncio.Event()
    later_item_started = asyncio.Event()
    release = [asyncio.Event() for _ in range(20)]
    started: list[int] = []

    async def operation(item: int) -> int:
        started.append(item)
        if len(started) == 2:
            first_wave.set()
        if item == 2:
            later_item_started.set()
        await release[item].wait()
        return item * 10

    existing = asyncio.all_tasks()
    batch = asyncio.create_task(map_concurrently(operation, list(range(20)), limit=2))
    try:
        await asyncio.wait_for(first_wave.wait(), 1)
        assert started == [0, 1]
        assert len(asyncio.all_tasks() - existing) == 3, (
            "20 项输入只能占用两个 worker 和批量调用本身，不能各建一个等待任务"
        )
        release[1].set()
        await asyncio.wait_for(later_item_started.wait(), 1)
        assert not release[0].is_set(), "后续输入应能在第一项未完成时继续处理"
        for event in release:
            event.set()
        assert await asyncio.wait_for(batch, 1) == list(range(0, 200, 10))
    finally:
        for event in release:
            event.set()
        batch.cancel()
        await asyncio.gather(batch, return_exceptions=True)


@pytest.mark.parametrize("limit", [0, -1])
async def test_concurrent_map_rejects_invalid_limit(limit: int) -> None:
    async def unused(item: str) -> str:
        pytest.fail("非法并发配置不得开始请求")

    with pytest.raises(ValueError, match="limit"):
        await map_concurrently(unused, ["x"], limit=limit)


async def test_concurrent_map_empty_input_starts_no_request() -> None:
    async def unused(item: str) -> str:
        pytest.fail("空输入不得开始请求")

    assert await map_concurrently(unused, [], limit=1) == []


async def test_cancel_suppressing_operation_does_not_start_more_queued_work() -> None:
    started: list[str] = []
    held = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    work_release = asyncio.Event()
    failure = OSError("upstream failed")

    async def operation(item: str) -> str:
        started.append(item)
        if item == "fail":
            await held.wait()
            raise failure
        held.set()
        try:
            await work_release.wait()
            return item
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return "completed during cleanup"

    batch = asyncio.create_task(
        map_concurrently(operation, ["fail", "held", "queued"], limit=2)
    )
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not batch.done()
        release.set()
        with pytest.raises(OSError) as caught:
            await asyncio.wait_for(asyncio.shield(batch), 1)
        assert caught.value is failure
        assert started == ["fail", "held"]
    finally:
        release.set()
        work_release.set()
        batch.cancel()
        await asyncio.gather(batch, return_exceptions=True)
