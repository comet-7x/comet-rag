from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

import pytest

from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.ports import Filter, SearchHit
from comet_rag.services.retrieval import (
    RetrievalResult,
    RetrievalService,
    SearchMode,
    SearchQuery,
)
from comet_rag.services.revision_recall import RevisionVisibilityError


class _Embedding(AsyncBaseEmbeddingModel):
    async def _embed(self, data, **kwargs) -> list[float]:
        return [1.0]


class _Channels:
    def __init__(
        self,
        dense: Callable[[], Awaitable[list[SearchHit]]],
        keyword: Callable[[], Awaitable[list[SearchHit]]],
    ) -> None:
        self.dense = dense
        self.keyword = keyword

    async def search(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        return await self.dense()

    async def search_keywords(
        self,
        kb_id: str,
        query: str,
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        return await self.keyword()


class _BlockedChannel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release_work = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def __call__(self) -> list[SearchHit]:
        self.started.set()
        try:
            await self.release_work.wait()
        finally:
            self.cleaning.set()
            await self.release_cleanup.wait()
            self.cleaned.set()
        return []


def _search(channels: _Channels) -> Awaitable[RetrievalResult]:
    service = RetrievalService(
        embedding_model=_Embedding(), vector_store=channels, keyword_search=channels
    )
    return service.search(
        SearchQuery(kb_id="kb", query="query", mode=SearchMode.HYBRID)
    )


async def _signal(event: asyncio.Event) -> None:
    # 超时只作死锁看门狗；顺序由 Event 决定，不靠调度耗时。
    await asyncio.wait_for(event.wait(), timeout=1.0)


@pytest.mark.parametrize("failing_channel", ["dense", "keyword"])
@pytest.mark.parametrize(
    "failure_type", [ValueError, RevisionVisibilityError, asyncio.CancelledError]
)
async def test_fatal_channel_cancels_and_drains_sibling_before_propagating(
    failing_channel: str, failure_type: type[BaseException]
) -> None:
    sibling = _BlockedChannel()
    failure = failure_type("fatal recall error")

    async def fail() -> list[SearchHit]:
        await sibling.started.wait()
        raise failure

    channels = (
        _Channels(fail, sibling)
        if failing_channel == "dense"
        else _Channels(sibling, fail)
    )
    request = asyncio.ensure_future(_search(channels))
    try:
        await _signal(sibling.cleaning)
        assert not request.done(), "查询不能在兄弟任务清理完成前结束"
        request.cancel("cancel while draining fatal recall")
        request.cancel("repeat cancellation")
        sibling.release_cleanup.set()
        with pytest.raises(failure_type) as caught:
            await request
        assert caught.value is failure
        assert sibling.cleaned.is_set()
    finally:
        sibling.release_work.set()
        sibling.release_cleanup.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("failing_channel", ["dense", "keyword"])
async def test_recoverable_channel_waits_for_sibling_result(
    failing_channel: str,
) -> None:
    started, failed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancelled = False

    async def fail() -> list[SearchHit]:
        await started.wait()
        failed.set()
        raise TimeoutError("temporary recall failure")

    async def survive() -> list[SearchHit]:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return [SearchHit("survivor", "visible result", 1.0)]

    channels = (
        _Channels(fail, survive)
        if failing_channel == "dense"
        else _Channels(survive, fail)
    )
    request = asyncio.ensure_future(_search(channels))
    try:
        await _signal(failed)
        assert not request.done()
        release.set()
        result = await request
        assert not cancelled
        assert [chunk.id for chunk in result.chunks] == ["survivor"]
        assert result.mode is (
            SearchMode.KEYWORD if failing_channel == "dense" else SearchMode.DENSE
        )
        assert result.degradations[0].reason == "channel_unavailable"
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_request_cancellation_drains_both_channels(repeat_cancel: bool) -> None:
    dense, keyword = _BlockedChannel(), _BlockedChannel()
    request = asyncio.ensure_future(_search(_Channels(dense, keyword)))
    try:
        await _signal(dense.started)
        await _signal(keyword.started)
        request.cancel("request cancelled")
        await _signal(dense.cleaning)
        await _signal(keyword.cleaning)
        assert not request.done()
        if repeat_cancel:
            request.cancel("cancel again during cleanup")
        dense.release_cleanup.set()
        keyword.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert dense.cleaned.is_set()
        assert keyword.cleaned.is_set()
    finally:
        dense.release_work.set()
        keyword.release_work.set()
        dense.release_cleanup.set()
        keyword.release_cleanup.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
