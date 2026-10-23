from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from comet_rag.core.concurrency import Gate, Overloaded
from comet_rag.engines.offload import run_sync
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.models.reranker.base import AsyncBaseReranker
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.ports import Filter, SearchHit
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.query_deadline import QueryDeadlineExceeded
from comet_rag.services.retrieval import (
    RetrievalService,
    ReturnLevel,
    SearchMode,
    SearchQuery,
)

HIT = SearchHit(
    "child", "visible child", 0.8, {"source_id": "source", "parent_id": "parent"}
)


async def hits() -> list[SearchHit]:
    return [HIT]


class Blocked:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.release_cleanup.set()

    async def __call__(self, *args) -> list[SearchHit]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            await self.release_cleanup.wait()
            self.cleaned.set()
        return []

    async def scores(self) -> list[float]:
        await self()
        return [0.9]


class Embedding(AsyncBaseEmbeddingModel):
    def __init__(self) -> None:
        self.calls = 0

    async def _embed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        return [1.0]


class Channels:
    def __init__(self, dense=hits, keyword=hits) -> None:
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


class Reranker(AsyncBaseReranker):
    def __init__(self, operation: Callable[[], Awaitable[list[float]]]) -> None:
        self.operation = operation
        self.calls = 0

    async def _score(self, query, documents, **kwargs) -> list[float]:
        self.calls += 1
        return await self.operation()


def service(channels=None, **kwargs) -> RetrievalService:
    channels = channels or Channels()
    return RetrievalService(
        embedding_model=kwargs.pop("embedding_model", Embedding()),
        vector_store=channels,
        keyword_search=channels,
        timeout_seconds=kwargs.pop("timeout_seconds", 0.02),
        **kwargs,
    )


def query(**kwargs) -> SearchQuery:
    return SearchQuery(kb_id="kb", query="query", **kwargs)


@pytest.mark.parametrize(
    "mode", [SearchMode.DENSE, SearchMode.KEYWORD, SearchMode.HYBRID]
)
async def test_no_completed_recall_is_deadline_failure_and_drained(mode) -> None:
    dense, keyword = Blocked(), Blocked()
    with pytest.raises(QueryDeadlineExceeded):
        await service(Channels(dense, keyword)).search(query(mode=mode))
    assert dense.cleaned.is_set() == (mode is not SearchMode.KEYWORD)
    assert keyword.cleaned.is_set() == (mode is not SearchMode.DENSE)


@pytest.mark.parametrize("slow", ["dense", "keyword"])
async def test_hybrid_keeps_completed_channel_and_skips_optional_io(slow) -> None:
    blocked = Blocked()
    channels = Channels(blocked, hits) if slow == "dense" else Channels(hits, blocked)
    reranker = Reranker(AsyncMock(return_value=[0.9]))
    documents = InMemoryDocumentStore()
    documents.get_parents = AsyncMock()
    result = await service(
        channels, reranker=reranker, document_store=documents
    ).search(query(mode=SearchMode.HYBRID, return_level=ReturnLevel.PARENT))
    assert blocked.cleaned.is_set()
    assert [chunk.id for chunk in result.chunks] == ["child"]
    assert result.mode.value == ("keyword" if slow == "dense" else "dense")
    assert [(d.stage.value, d.reason) for d in result.degradations] == [
        (slow, "deadline_exceeded"),
        ("reranker", "deadline_exceeded"),
        ("parent", "deadline_exceeded"),
    ]
    assert not result.reranked
    assert reranker.calls == 0
    documents.get_parents.assert_not_awaited()


@pytest.mark.parametrize("stage", ["reranker", "parent"])
async def test_optional_io_timeout_preserves_visible_child(stage) -> None:
    blocked = Blocked()
    documents = InMemoryDocumentStore()
    if stage == "reranker":
        sut = service(reranker=Reranker(blocked.scores))
    else:
        documents.get_parents = AsyncMock(side_effect=blocked.__call__)
        sut = service(document_store=documents)
    result = await sut.search(query(return_level=ReturnLevel.PARENT))
    assert [chunk.id for chunk in result.chunks] == ["child"]
    assert not result.reranked
    assert any(
        d.stage.value == stage and d.reason == "deadline_exceeded"
        for d in result.degradations
    )
    assert blocked.cleaned.is_set()


async def test_recall_time_is_not_reset_for_reranking() -> None:
    async def recall() -> list[SearchHit]:
        await asyncio.sleep(0.06)
        return [HIT]

    async def rank() -> list[float]:
        await asyncio.sleep(0.06)
        return [0.99]

    result = await service(
        Channels(recall), reranker=Reranker(rank), timeout_seconds=0.1
    ).search(query())
    assert not result.reranked
    assert result.chunks[0].score == 0.8
    assert result.degradations[0].reason == "deadline_exceeded"


async def test_model_queue_wait_consumes_budget_and_releases_waiter() -> None:
    gate = Gate(limit=1, max_waiting=1, acquire_timeout=1)
    embedding = Embedding()
    embedding.bind_gate(gate)
    async with gate:
        with pytest.raises(QueryDeadlineExceeded):
            await service(embedding_model=embedding).search(query())
        assert gate.stats.waiting == 0
        assert gate.stats.in_flight == 1
        assert embedding.calls == 0
    assert gate.stats.in_flight == 0
    assert await embedding.embed_query("next") == [1.0]


async def test_earlier_gate_rejection_stays_overloaded() -> None:
    gate = Gate(limit=1, max_waiting=1, acquire_timeout=0.01)
    embedding = Embedding()
    embedding.bind_gate(gate)
    async with gate:
        with pytest.raises(Overloaded):
            await service(embedding_model=embedding, timeout_seconds=1).search(query())
        assert gate.stats.waiting == 0


async def test_visibility_timeout_never_returns_unverified_candidates() -> None:
    blocked = Blocked()
    documents = InMemoryDocumentStore()
    documents.get_active_revisions = AsyncMock(side_effect=blocked.__call__)
    with pytest.raises(QueryDeadlineExceeded):
        await service(document_store=documents).search(query())
    assert blocked.cleaned.is_set()


@pytest.mark.parametrize("stage", ["dense", "reranker", "parent"])
async def test_caller_cancellation_during_timeout_drain_is_not_degradation(
    stage,
) -> None:
    blocked = Blocked()
    blocked.release_cleanup.clear()
    documents = InMemoryDocumentStore()
    if stage == "dense":
        sut = service(Channels(blocked))
    elif stage == "reranker":
        sut = service(reranker=Reranker(blocked.scores))
    else:
        documents.get_parents = AsyncMock(side_effect=blocked.__call__)
        sut = service(document_store=documents)
    request = asyncio.create_task(sut.search(query(return_level=ReturnLevel.PARENT)))
    try:
        await asyncio.wait_for(blocked.cleaning.wait(), 1)
        assert not request.done()
        request.cancel("caller cancellation during drain")
        await asyncio.sleep(0)
        request.cancel("repeat caller cancellation")
        assert not request.done()
        blocked.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert blocked.cleaned.is_set()
    finally:
        blocked.release_cleanup.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_thread_bridge_keeps_gate_until_work_finishes_after_deadline() -> None:
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    gate = Gate(limit=1, max_waiting=1)

    class ThreadEmbedding(Embedding):
        async def _embed(self, data, **kwargs) -> list[float]:
            def work() -> list[float]:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(2), "thread watchdog"
                return [1.0]

            return await run_sync(work)

    embedding = ThreadEmbedding()
    embedding.bind_gate(gate)
    request = asyncio.create_task(service(embedding_model=embedding).search(query()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0.04)
        assert not request.done()
        assert gate.stats.in_flight == 1
        release.set()
        with pytest.raises(QueryDeadlineExceeded):
            await request
        assert gate.stats.in_flight == 0
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_budget_is_not_reused_by_later_queries() -> None:
    dense = Blocked()
    channels = Channels(dense)
    sut = service(channels)
    with pytest.raises(QueryDeadlineExceeded):
        await sut.search(query())
    channels.dense = hits
    result = await sut.search(query())
    assert [chunk.id for chunk in result.chunks] == ["child"]


@pytest.mark.parametrize("invalid", [0, -1, True, float("inf"), float("nan"), "1"])
def test_service_rejects_invalid_budgets(invalid) -> None:
    with pytest.raises(ValueError):
        service(timeout_seconds=invalid)


async def test_parent_lookup_uses_remaining_budget_after_successful_rerank() -> None:
    async def rank() -> list[float]:
        await asyncio.sleep(0.06)
        return [0.99]

    async def parents(*args) -> dict:
        await asyncio.sleep(0.06)
        return {}

    documents = InMemoryDocumentStore()
    documents.get_parents = AsyncMock(side_effect=parents)
    result = await service(
        reranker=Reranker(rank), document_store=documents, timeout_seconds=0.1
    ).search(query(return_level=ReturnLevel.PARENT))
    assert result.reranked
    assert result.chunks[0].id == "child"
    assert result.chunks[0].score == 0.99
    assert [(d.stage.value, d.reason) for d in result.degradations] == [
        ("parent", "deadline_exceeded")
    ]


async def test_knowledge_base_check_consumes_budget_before_any_recall() -> None:
    blocked = Blocked()
    knowledge_base = cast(
        "KnowledgeBaseService",
        SimpleNamespace(resolve_for_search=AsyncMock(side_effect=blocked.__call__)),
    )
    embedding = Embedding()
    with pytest.raises(QueryDeadlineExceeded) as caught:
        await service(embedding_model=embedding, knowledge_base=knowledge_base).search(
            query()
        )
    assert caught.value.stage == "knowledge_base"
    assert embedding.calls == 0
    assert blocked.cleaned.is_set()


async def test_provider_timeout_is_not_reported_as_shared_deadline() -> None:
    async def timeout() -> list[float]:
        raise TimeoutError("provider socket timeout")

    result = await service(reranker=Reranker(timeout), timeout_seconds=1).search(
        query()
    )
    assert result.degradations[0].reason == "request_failed"
    assert result.degradations[0].error_type == "TimeoutError"
