from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from pymilvus import AsyncMilvusClient
from pymilvus.milvus_client.index import IndexParams

from comet_rag.infrastructure.persistence.vector_store.milvus import AsyncMilvusStore
from comet_rag.ports import VectorRecord

pytestmark = pytest.mark.integration

KB = "native-async-lifecycle"


@pytest.fixture
async def native_store(
    milvus_uri: str, milvus_database: str
) -> AsyncIterator[AsyncMilvusStore]:
    store = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=f"ctnative_{uuid4().hex[:12]}",
        consistency_level="Strong",
    )
    try:
        yield store
    finally:
        # 仅清理本用例的固定 KB + 随机前缀；即使建索引失败也不能遗留半成品。
        try:
            await store.drop_collection(KB)
        finally:
            await store.close()


def _indexes(*, sparse: bool = True) -> IndexParams:
    indexes = AsyncMilvusClient.prepare_index_params()
    indexes.add_index(
        field_name="dense_vector", index_type="AUTOINDEX", metric_type="COSINE"
    )
    if sparse:
        indexes.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
    return indexes


def _record() -> VectorRecord:
    return VectorRecord(
        id="survivor",
        text="原生异步索引恢复测试",
        embedding=[1.0, 0.0],
    )


async def test_native_index_creation_is_idempotent(
    native_store: AsyncMilvusStore,
) -> None:
    """续建依赖服务端同参数幂等；不能只在 fake client 上证明这个前提。"""
    await native_store.ensure_collection(KB, dim=2)
    client = native_store._client
    assert isinstance(client, AsyncMilvusClient)
    for _ in range(2):
        await client.create_index(native_store._name(KB), index_params=_indexes())

    await native_store.upsert(KB, [_record()])
    assert await native_store.count(KB) == 1
    assert [hit.id for hit in await native_store.search_keywords(KB, "索引恢复")] == [
        "survivor"
    ]


async def test_lost_index_response_preserves_another_clients_data(
    native_store: AsyncMilvusStore,
    milvus_uri: str,
    milvus_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实服务端已完成并已有写入，再丢失响应；回滚绝不能删除共享集合。"""
    writer = native_store
    reader = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=writer._prefix,
        consistency_level="Strong",
    )
    create_index = writer._client.create_index
    failure = TimeoutError("index response lost after server completion")

    async def lose_response(collection_name: str, index_params: IndexParams) -> None:
        await create_index(collection_name, index_params=index_params)
        await reader.ensure_collection(KB, dim=2)
        await reader.upsert(KB, [_record()])
        raise failure

    monkeypatch.setattr(writer._client, "create_index", lose_response)
    try:
        with pytest.raises(TimeoutError) as caught:
            await writer.ensure_collection(KB, dim=2)
        assert caught.value is failure
        assert await reader._client.has_collection(reader._name(KB))
        assert await reader.count(KB) == 1
        assert [hit.id for hit in await reader.search(KB, [1.0, 0.0])] == ["survivor"]

        monkeypatch.setattr(writer._client, "create_index", create_index)
        await writer.ensure_collection(KB, dim=2)
    finally:
        await reader.close()
    # SDK 连接管理器可能复用连接；关闭一个客户端不应关闭另一个的引用。
    assert await writer.count(KB) == 1


async def test_cancelled_partial_index_creation_is_recoverable_after_restart(
    native_store: AsyncMilvusStore,
    milvus_uri: str,
    milvus_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只建好 dense 就取消，丢弃本地状态后仍可安全补齐 sparse。"""
    writer = native_store
    create_index = writer._client.create_index
    dense_ready = asyncio.Event()

    async def stop_after_dense(collection_name: str, index_params: IndexParams) -> None:
        await create_index(collection_name, index_params=_indexes(sparse=False))
        dense_ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(writer._client, "create_index", stop_after_dense)
    creating = asyncio.create_task(writer.ensure_collection(KB, dim=2))
    try:
        await asyncio.wait_for(dense_ready.wait(), timeout=30)
        creating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creating
    finally:
        creating.cancel()
        await asyncio.gather(creating, return_exceptions=True)

    assert await writer._client.has_collection(writer._name(KB))
    assert KB not in writer._dims
    restarted = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=writer._prefix,
        consistency_level="Strong",
    )
    try:
        await restarted.ensure_collection(KB, dim=2)
        await restarted.upsert(KB, [_record()])
        assert await restarted.count(KB) == 1
        assert [hit.id for hit in await restarted.search_keywords(KB, "索引恢复")] == [
            "survivor"
        ]
    finally:
        await restarted.close()
