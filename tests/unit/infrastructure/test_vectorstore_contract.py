"""`InMemoryVectorStore` 跑 VectorStore 契约。

同一套契约将来由 `MilvusStore`（T21）在 tests/integration/ 下再跑一遍。
"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.vectorstore import BaseVectorStore, InMemoryVectorStore
from tests.contracts.vector_store import VectorStoreContract


class TestInMemoryVectorStore(VectorStoreContract):
    @pytest.fixture
    async def store(self) -> BaseVectorStore:
        return InMemoryVectorStore()
