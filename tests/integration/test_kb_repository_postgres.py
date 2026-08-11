"""`PostgresKnowledgeBaseRepository` 跑与内存实现**同一套**契约。

同一套测试跑两种实现，是"换存储时行为不变"这句承诺唯一的兑现手段。
本文件除了 fixture 之外不写任何用例 —— 写了就说明契约没覆盖到，
该补的是契约而不是这里。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from comet_rag.infrastructure.database import Database
from comet_rag.infrastructure.database.kb_repository import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.knowledge_base import KnowledgeBaseRepository
from tests.contracts.knowledge_base import KnowledgeBaseRepositoryContract

pytestmark = pytest.mark.integration


class TestPostgresKnowledgeBaseRepository(KnowledgeBaseRepositoryContract):
    @pytest.fixture
    async def repo(self, database: Database) -> AsyncIterator[KnowledgeBaseRepository]:
        await _require_migrated(database)
        # 每个用例前清空：残留数据会让"按创建时间倒序"之类的断言随机失败，
        # 而且失败与否取决于用例执行顺序 —— 那类问题排查成本极高。
        await _truncate(database)
        yield PostgresKnowledgeBaseRepository(database)
        await _truncate(database)


async def _require_migrated(database: Database) -> None:
    async with database.session() as session:
        exists = (
            await session.execute(text("SELECT to_regclass('knowledge_bases')"))
        ).scalar_one()
    if exists is None:
        pytest.skip("knowledge_bases 表不存在，先跑 `uv run alembic upgrade head`")


async def _truncate(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(text("TRUNCATE TABLE knowledge_bases"))
