"""`MilvusStore` 跑与 `InMemoryVectorStore` **同一套**契约。

本契约存在的头号理由就是本文件（plan R1）：Milvus 默认一致性下
写入后立刻检索命中 0 条，而这个差异不体现在任何方法签名上 ——
内存实现全绿、换 Milvus 静默失效。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest

from comet_rag.infrastructure.vectorstore import BaseVectorStore
from tests.contracts.vector_store import VectorStoreContract

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(milvus_uri: str) -> AsyncIterator[BaseVectorStore]:
    from comet_rag.infrastructure.vectorstore.milvus import MilvusStore

    # 每轮用独立前缀，避免上一轮残留的 collection 影响断言
    vs = MilvusStore(endpoint=milvus_uri, prefix="cttest")
    try:
        yield vs
    finally:
        for kb in ("kb-contract", "kb-idem", "kb-dim", "kb-other", "kb-empty"):
            # 清理尽力而为：某个 collection 删不掉不该让整轮测试红掉
            with contextlib.suppress(Exception):
                await vs.adrop_collection(kb)
        await vs.aclose()


class TestMilvusVectorStore(VectorStoreContract):
    @pytest.fixture
    async def store(self, store: BaseVectorStore) -> BaseVectorStore:  # noqa: PT004
        return store
