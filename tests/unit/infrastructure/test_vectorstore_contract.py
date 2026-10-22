"""`AsyncInMemoryVectorStore` 跑 VectorStore 契约。

同一套契约将来由 `AsyncMilvusStore`（T21）在 tests/integration/ 下再跑一遍。
"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import AsyncVectorStore
from tests.contracts.vector_store import VectorStoreContract


class TestInMemoryVectorStore(VectorStoreContract):
    @pytest.fixture
    async def store(self) -> AsyncVectorStore:
        return AsyncInMemoryVectorStore()
