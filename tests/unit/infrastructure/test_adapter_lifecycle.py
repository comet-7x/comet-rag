from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import cast

import httpx
import pytest

from comet_rag.infrastructure.extractors import mineru
from comet_rag.infrastructure.models.embedding import qwen3_vl as embedding
from comet_rag.infrastructure.models.reranker import qwen3_vl as reranker
from comet_rag.infrastructure.persistence.vector_store import milvus

ADAPTERS = ("embedding", "reranker", "mineru")


class _SyncClient:
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self.events, self.error = events, error

    def close(self) -> None:
        self.events.append("sync.close")
        if self.error is not None:
            raise self.error


class _AsyncClient:
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self.events, self.error = events, error

    async def aclose(self) -> None:
        self.events.append("async.close")
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        await self.aclose()


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
    sync_factory: Callable[..., object],
    async_factory: Callable[..., object],
) -> None:
    target = {"embedding": embedding, "reranker": reranker, "mineru": httpx}[adapter]
    monkeypatch.setattr(target, "Client", sync_factory)
    monkeypatch.setattr(target, "AsyncClient", async_factory)


def _build(adapter: str, asynchronous: bool, client: object = None):
    sync = cast("httpx.Client | None", client)
    async_client = cast("httpx.AsyncClient | None", client)
    if adapter == "embedding":
        if asynchronous:
            return embedding.AsyncQwen3VLEmbeddingModel(
                "http://model.invalid", "embedding", "EMPTY", client=async_client
            )
        return embedding.Qwen3VLEmbeddingModel(
            "http://model.invalid", "embedding", "EMPTY", client=sync
        )
    if adapter == "reranker":
        if asynchronous:
            return reranker.AsyncQwen3VLReranker(
                "http://model.invalid", "reranker", "EMPTY", client=async_client
            )
        return reranker.Qwen3VLReranker(
            "http://model.invalid", "reranker", "EMPTY", client=sync
        )
    if asynchronous:
        return mineru.AsyncMinerUDocumentExtractor(
            "https://mineru.invalid", client=async_client
        )
    return mineru.MinerUDocumentExtractor("https://mineru.invalid", client=sync)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_constructor_failure_never_allocates_opposite_mode_client(
    monkeypatch: pytest.MonkeyPatch, adapter: str, asynchronous: bool
) -> None:
    error = RuntimeError("client construction failed")
    calls: list[str] = []

    def own(**kwargs: object) -> object:
        calls.append("own")
        raise error

    def opposite(**kwargs: object) -> object:
        pytest.fail("an instance must own only its execution model's client")

    _patch_clients(
        monkeypatch,
        adapter,
        opposite if asynchronous else own,
        own if asynchronous else opposite,
    )
    with pytest.raises(RuntimeError) as caught:
        _build(adapter, asynchronous)
    assert caught.value is error
    assert calls == ["own"]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_close_delegates_only_to_owned_client(
    monkeypatch: pytest.MonkeyPatch, adapter: str, asynchronous: bool, close_fails: bool
) -> None:
    events: list[str] = []
    error = OSError("close failed") if close_fails else None
    client = _AsyncClient(events, error) if asynchronous else _SyncClient(events, error)

    def opposite(**kwargs: object) -> object:
        pytest.fail("opposite client must not be constructed")

    _patch_clients(
        monkeypatch,
        adapter,
        opposite if asynchronous else lambda **_: client,
        (lambda **_: client) if asynchronous else opposite,
    )
    resource = _build(adapter, asynchronous)

    async def close() -> None:
        result = resource.close()
        if inspect.isawaitable(result):
            await result

    if error is None:
        await close()
    else:
        with pytest.raises(OSError) as caught:
            await close()
        assert caught.value is error
    assert events == ["async.close" if asynchronous else "sync.close"]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_injected_client_remains_owned_by_caller(
    monkeypatch: pytest.MonkeyPatch, adapter: str, asynchronous: bool
) -> None:
    events: list[str] = []
    client = _AsyncClient(events) if asynchronous else _SyncClient(events)

    def forbidden(**kwargs: object) -> object:
        pytest.fail("injected client must be reused")

    _patch_clients(monkeypatch, adapter, forbidden, forbidden)
    resource = _build(adapter, asynchronous, client)
    result = resource.close()
    if inspect.isawaitable(result):
        await result
    assert events == []


@pytest.mark.parametrize("close_fails", [False, True])
async def test_milvus_owns_one_native_async_client(
    monkeypatch: pytest.MonkeyPatch, close_fails: bool
) -> None:
    events: list[str] = []
    error = OSError("close failed") if close_fails else None
    client = _AsyncClient(events, error)
    constructors: list[dict[str, object]] = []

    def create_client(**kwargs: object) -> _AsyncClient:
        constructors.append(kwargs)
        return client

    monkeypatch.setattr(milvus, "AsyncMilvusClient", create_client)
    store = milvus.AsyncMilvusStore(database_name="lifecycle")
    assert constructors == [
        {"uri": "http://localhost:19530", "token": "", "db_name": "lifecycle"}
    ]
    assert events == []
    if error is None:
        await store.close()
    else:
        with pytest.raises(OSError) as caught:
            await store.close()
        assert caught.value is error
    assert events == ["async.close"]
