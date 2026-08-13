"""`InMemoryTaskStore` 跑 TaskStore 契约。

同一套契约将来由 `PostgresTaskStore`（T20）在 tests/integration/ 下再跑一遍。
"""

from __future__ import annotations

import pytest

from comet_rag.tasks import InMemoryTaskStore, TaskStore
from tests.contracts.task_store import TaskStoreContract


class TestInMemoryTaskStore(TaskStoreContract):
    @pytest.fixture
    async def store(self) -> TaskStore:
        return InMemoryTaskStore()
