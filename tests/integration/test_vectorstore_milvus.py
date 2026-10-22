"""`AsyncMilvusStore` 跑与 `AsyncInMemoryVectorStore` **同一套**契约。

本契约存在的头号理由就是本文件（plan R1）：Milvus 默认一致性下
写入后立刻检索命中 0 条，而这个差异不体现在任何方法签名上 ——
内存实现全绿、换 Milvus 静默失效。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

import pytest

from comet_rag.ports import (
    AsyncKeywordSearchPort,
    AsyncVectorStore,
    CollectionSchemaMismatch,
    VectorRecord,
)
from tests.contracts.keyword_search import KeywordSearchContract, KeywordSearchStore
from tests.contracts.retrieval_scenario import assert_retrieval_modes
from tests.contracts.vector_store import VectorStoreContract

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(
    milvus_uri: str, milvus_database: str
) -> AsyncIterator[AsyncVectorStore]:
    from comet_rag.infrastructure.persistence.vector_store.milvus import (
        AsyncMilvusStore,
    )

    # 随机前缀让清理范围能被精确证明，不会碰到其他测试或业务 collection。
    vs = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=f"cttest_{uuid4().hex[:12]}",
    )
    try:
        yield vs
    finally:
        # 只清理本 fixture 实际接触过的 collection；随机前缀已隔离其他数据。
        touched = set(vs._dims) | vs._created  # noqa: SLF001
        for kb in touched:
            # 清理尽力而为：某个 collection 删不掉不该让整轮测试红掉
            with contextlib.suppress(Exception):
                await vs.drop_collection(kb)
        await vs.close()


class TestMilvusVectorStore(VectorStoreContract):
    @pytest.fixture
    async def store(self, store: AsyncVectorStore) -> AsyncVectorStore:  # noqa: PT004
        return store


class TestMilvusKeywordSearch(KeywordSearchContract):
    @pytest.fixture
    async def store(  # noqa: PT004
        self, store: AsyncVectorStore
    ) -> KeywordSearchStore:
        return cast("KeywordSearchStore", store)


async def test_strong_reads_see_revision_from_independent_client(
    milvus_uri: str,
    milvus_database: str,
) -> None:
    from comet_rag.infrastructure.persistence.vector_store.milvus import (
        AsyncMilvusStore,
    )

    prefix = f"ctrevision_{uuid4().hex[:12]}"
    writer = AsyncMilvusStore(
        endpoint=milvus_uri, database_name=milvus_database, prefix=prefix
    )
    reader = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=prefix,
        consistency_level="Strong",
    )
    kb = "revision-cross-client"
    try:
        # 故意复用 Session 创建的 collection，证明读请求真正覆盖 collection 默认值。
        await writer.ensure_collection(kb, dim=2)
        await writer.upsert(
            kb,
            [
                VectorRecord(
                    id="new-child",
                    text="量子纠缠实验报告",
                    embedding=[1.0, 0.0],
                    metadata={"source_id": "source", "document_revision": "new"},
                )
            ],
        )
        exact = {"source_id": "source", "document_revision": "new"}
        assert await reader.count(kb, filter=exact) == 1
        assert [
            hit.id for hit in await reader.search(kb, [1.0, 0.0], filter=exact)
        ] == ["new-child"]
        assert [
            hit.id for hit in await reader.search_keywords(kb, "量子纠缠", filter=exact)
        ] == ["new-child"]
    finally:
        try:
            await writer.drop_collection(kb)
        finally:
            await reader.close()
            await writer.close()


async def test_bm25_schema_handles_chinese_and_english_terms(
    store: AsyncVectorStore,
) -> None:
    """T3 直接验证服务端 analyzer/function/index；Port 行为在 T4 定义。"""
    await store.ensure_collection("kb-bm25-schema", dim=4)
    await store.upsert(
        "kb-bm25-schema",
        [
            VectorRecord(
                id="zh",
                text="星际量子纠缠实验报告",
                embedding=[1.0, 0.0, 0.0, 0.0],
            ),
            VectorRecord(
                id="en",
                text="Rust TraitObject memory layout",
                embedding=[0.0, 1.0, 0.0, 0.0],
            ),
        ],
    )

    client = store._client  # type: ignore[attr-defined]  # noqa: SLF001
    name = store._name("kb-bm25-schema")  # type: ignore[attr-defined]  # noqa: SLF001
    for query, expected in (("量子纠缠", "zh"), ("TraitObject", "en")):
        results = await client.search(
            name,
            data=[query],
            anns_field="sparse_vector",
            search_params={"metric_type": "BM25"},
            limit=2,
            output_fields=["text"],
        )
        assert results and results[0]
        assert results[0][0]["id"] == expected


async def test_retrieval_service_runs_all_modes_against_real_milvus(
    store: AsyncVectorStore,
) -> None:
    """真实 analyzer、两路召回、RRF 和结构化过滤必须在同一链路工作。"""
    await assert_retrieval_modes(store, cast("AsyncKeywordSearchPort", store))


async def test_real_legacy_schema_is_rejected_without_deletion(
    store: AsyncVectorStore,
    milvus_uri: str,
    milvus_database: str,
) -> None:
    """旧 collection 属于用户数据；兼容检查失败也绝不能自动迁移或删除。"""
    from pymilvus import DataType

    from comet_rag.infrastructure.persistence.vector_store.milvus import (
        AsyncMilvusStore,
    )

    kb = "kb-real-legacy-schema"
    raw: Any = store
    name = raw._name(kb)  # noqa: SLF001
    schema = raw._client.create_schema(  # noqa: SLF001
        auto_id=False, enable_dynamic_field=False
    )
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("metadata", DataType.JSON)
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=4)
    schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
    await raw._client.create_collection(  # noqa: SLF001
        name,
        schema=schema,  # noqa: SLF001
    )
    # fixture 只回收本次测试明确登记的 collection。
    raw._created.add(kb)  # noqa: SLF001

    with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
        await store.ensure_collection(kb, dim=4)

    # 模拟服务重启：新实例没有 _dims 缓存，直接走读路径也必须得到同一个
    # 可操作的 schema 错误，而不是泄漏 Milvus 异常或被 hybrid 当成通道抖动。
    restarted = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=raw._prefix,  # noqa: SLF001
    )
    try:
        with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
            await restarted.search(kb, [1.0, 0.0, 0.0, 0.0])
        with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
            await restarted.search_keywords(kb, "量子")
    finally:
        await restarted.close()

    assert await raw._client.has_collection(name)  # noqa: SLF001


async def test_two_instances_concurrently_restore_a_released_collection(
    store: AsyncVectorStore,
    milvus_uri: str,
    milvus_database: str,
) -> None:
    """两个进程观察到 NotLoad 时都能幂等加载，且只在 Loaded 后返回。"""
    from pymilvus.client.types import LoadState

    from comet_rag.infrastructure.persistence.vector_store.milvus import (
        AsyncMilvusStore,
    )

    kb = "kb-concurrent-load"
    raw: Any = store
    await raw.ensure_collection(kb, dim=4)
    name = raw._name(kb)  # noqa: SLF001
    await raw._client.release_collection(name)  # noqa: SLF001

    second = AsyncMilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=raw._prefix,  # noqa: SLF001
    )
    try:
        await asyncio.gather(
            raw.ensure_collection(kb, dim=4),
            second.ensure_collection(kb, dim=4),
        )
        state = await raw._client.get_load_state(name)  # noqa: SLF001
        assert state["state"] == LoadState.Loaded
        assert raw._dims[kb] == 4  # noqa: SLF001
        assert second._dims[kb] == 4  # noqa: SLF001
    finally:
        await second.close()
