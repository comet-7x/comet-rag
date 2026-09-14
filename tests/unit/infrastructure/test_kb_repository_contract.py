"""`InMemoryKnowledgeBaseRepository` 跑 KB 仓储契约。

`PostgresKnowledgeBaseRepository` 在 tests/integration/ 下跑同一套。
"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.ports.knowledge_base import KnowledgeBaseRepository
from tests.contracts.knowledge_base import KnowledgeBaseRepositoryContract


class TestInMemoryKnowledgeBaseRepository(KnowledgeBaseRepositoryContract):
    @pytest.fixture
    async def repo(self) -> KnowledgeBaseRepository:
        return InMemoryKnowledgeBaseRepository()
