"""`MilvusStore` 跑与 `InMemoryVectorStore` **同一套**契约。

本契约存在的头号理由就是本文件（plan R1）：Milvus 默认一致性下
写入后立刻检索命中 0 条，而这个差异不体现在任何方法签名上 ——
内存实现全绿、换 Milvus 静默失效。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import cast
from uuid import uuid4

import pytest

from comet_rag.ports import BaseVectorStore, VectorRecord
from tests.contracts.keyword_search import KeywordSearchContract, KeywordSearchStore
from tests.contracts.vector_store import VectorStoreContract

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(
    milvus_uri: str, milvus_database: str
) -> AsyncIterator[BaseVectorStore]:
    from comet_rag.infrastructure.vectorstore.milvus import MilvusStore

    # 随机前缀让清理范围能被精确证明，不会碰到其他测试或业务 collection。
    vs = MilvusStore(
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
                await vs.adrop_collection(kb)
        await vs.aclose()


class TestMilvusVectorStore(VectorStoreContract):
    @pytest.fixture
    async def store(self, store: BaseVectorStore) -> BaseVectorStore:  # noqa: PT004
        return store


class TestMilvusKeywordSearch(KeywordSearchContract):
    @pytest.fixture
    async def store(  # noqa: PT004
        self, store: BaseVectorStore
    ) -> KeywordSearchStore:
        return cast("KeywordSearchStore", store)


async def test_bm25_schema_handles_chinese_and_english_terms(
    store: BaseVectorStore,
) -> None:
    """T3 直接验证服务端 analyzer/function/index；Port 行为在 T4 定义。"""
    await store.aensure_collection("kb-bm25-schema", dim=4)
    await store.aupsert(
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

    client = store._async  # type: ignore[attr-defined]  # noqa: SLF001
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
