"""`InProcessExecutor` 跑 TaskExecutor 契约。

同一套契约将来由 `ArqExecutor`（T22）在 tests/integration/ 下再跑一遍。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from comet_rag.tasks import InMemoryTaskStore, InProcessExecutor, TaskStore
from tests.contracts.task_executor import TaskExecutorContract


class TestInProcessExecutor(TaskExecutorContract):
    max_concurrency = 3

    @pytest.fixture
    async def store(self) -> TaskStore:
        return InMemoryTaskStore()

    @pytest.fixture
    async def executor(self, store: TaskStore) -> AsyncIterator[InProcessExecutor]:
        # retry_backoff 压到 10ms：语义由断言覆盖，不靠真等
        ex = InProcessExecutor(
            store, max_concurrency=self.max_concurrency, retry_backoff=0.01
        )
        yield ex
        await ex.shutdown(timeout=5.0)
