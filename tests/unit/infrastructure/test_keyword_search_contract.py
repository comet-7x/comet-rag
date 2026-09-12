"""内存关键词检索实现及契约保护网。"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from comet_rag.ports import Filter, SearchHit
from tests.contracts.keyword_search import (
    DIM,
    KB,
    KeywordSearchContract,
    KeywordSearchStore,
    keyword_records,
)


class TestInMemoryKeywordSearch(KeywordSearchContract):
    @pytest.fixture
    async def store(self) -> KeywordSearchStore:
        return InMemoryVectorStore()


class _FilterIgnoringStore(InMemoryVectorStore):
    async def asearch_keywords(
        self,
        kb_id: str,
        query: str,
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        del filter
        return await super().asearch_keywords(
            kb_id, query, top_k=top_k, filter=None
        )


async def test_contract_detects_an_implementation_that_ignores_filter() -> None:
    """故意注入过滤缺陷，证明契约断言会失败而不是恒真。"""
    store = _FilterIgnoringStore()
    await store.aensure_collection(KB, dim=DIM)
    await store.aupsert(KB, keyword_records())

    with pytest.raises(AssertionError):
        await KeywordSearchContract().test_filter_by_equality(store, KB)
