"""`MilvusStore` 跑与 `InMemoryVectorStore` **同一套**契约。

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

from comet_rag.ports import BaseVectorStore, CollectionSchemaMismatch, VectorRecord
from comet_rag.services.retrieval import RetrievalService, SearchMode, SearchQuery
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


class _FixedQueryEmbedding:
    """把语义改写稳定映射到固定向量，隔离真实 embedding 服务的波动。"""

    async def aembed_query(self, text: str) -> list[float]:
        if "弯月形" in text:
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0, 0.0]


async def test_retrieval_service_runs_all_modes_against_real_milvus(
    store: BaseVectorStore,
) -> None:
    """真实 analyzer、两路召回、RRF 和结构化过滤必须在同一链路工作。"""
    kb = "kb-real-hybrid"
    await store.aensure_collection(kb, dim=4)
    await store.aupsert(
        kb,
        [
            VectorRecord(
                id="semantic",
                text="香蕉适合温暖气候种植",
                embedding=[1.0, 0.0, 0.0, 0.0],
                metadata={"language": "zh", "kind": "fruit"},
            ),
            VectorRecord(
                id="exact",
                text="Rust TraitObject memory layout",
                embedding=[0.0, 1.0, 0.0, 0.0],
                metadata={"language": "en", "kind": "code"},
            ),
            VectorRecord(
                id="noise",
                text="数据库连接池容量规划",
                embedding=[0.0, 0.0, 1.0, 0.0],
                metadata={"language": "zh", "kind": "ops"},
            ),
        ],
    )
    service = RetrievalService(
        # 测试替身只实现本链路实际消费的查询能力；完整模型契约另有独立测试。
        embedding_model=cast("Any", _FixedQueryEmbedding()),
        vector_store=store,
        keyword_search=cast("Any", store),
    )

    dense = await service.search(
        SearchQuery(
            kb_id=kb,
            query="弯月形水果如何栽培",
            mode=SearchMode.DENSE,
            rerank=False,
            top_k=1,
        )
    )
    assert [chunk.id for chunk in dense.chunks] == ["semantic"]
    assert dense.chunks[0].vector_score is not None

    keyword = await service.search(
        SearchQuery(
            kb_id=kb,
            query="TraitObject",
            mode=SearchMode.KEYWORD,
            filter={"language": "en"},
            rerank=False,
            top_k=1,
        )
    )
    assert [chunk.id for chunk in keyword.chunks] == ["exact"]
    assert keyword.chunks[0].keyword_score is not None

    hybrid = await service.search(
        SearchQuery(
            kb_id=kb,
            query="TraitObject 弯月形",
            mode=SearchMode.HYBRID,
            rerank=False,
            top_k=3,
        )
    )
    assert {chunk.id for chunk in hybrid.chunks} >= {"semantic", "exact"}
    assert hybrid.mode is SearchMode.HYBRID
    assert hybrid.chunks[0].fusion_score is not None


async def test_real_legacy_schema_is_rejected_without_deletion(
    store: BaseVectorStore,
    milvus_uri: str,
    milvus_database: str,
) -> None:
    """旧 collection 属于用户数据；兼容检查失败也绝不能自动迁移或删除。"""
    from pymilvus import DataType

    from comet_rag.infrastructure.vectorstore.milvus import MilvusStore

    kb = "kb-real-legacy-schema"
    raw: Any = store
    name = raw._name(kb)  # noqa: SLF001
    schema = raw._sync.create_schema(  # noqa: SLF001
        auto_id=False, enable_dynamic_field=False
    )
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("metadata", DataType.JSON)
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=4)
    schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
    await asyncio.to_thread(
        raw._sync.create_collection,
        name,
        schema=schema,  # noqa: SLF001
    )
    # fixture 只回收本次测试明确登记的 collection。
    raw._created.add(kb)  # noqa: SLF001

    with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
        await store.aensure_collection(kb, dim=4)

    # 模拟服务重启：新实例没有 _dims 缓存，直接走读路径也必须得到同一个
    # 可操作的 schema 错误，而不是泄漏 Milvus 异常或被 hybrid 当成通道抖动。
    restarted = MilvusStore(
        endpoint=milvus_uri,
        database_name=milvus_database,
        prefix=raw._prefix,  # noqa: SLF001
    )
    try:
        with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
            await restarted.asearch(kb, [1.0, 0.0, 0.0, 0.0])
        with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
            await restarted.asearch_keywords(kb, "量子")
    finally:
        await restarted.aclose()

    assert await asyncio.to_thread(raw._sync.has_collection, name)  # noqa: SLF001
