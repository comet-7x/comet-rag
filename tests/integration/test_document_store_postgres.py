"""PostgresDocumentStore 运行与内存实现相同的契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.ports.document_store import DocumentStore
from tests.contracts.document_store import DocumentStoreContract, make_draft
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration


class TestPostgresDocumentStore(DocumentStoreContract):
    @pytest.fixture
    async def store(self, database: Database) -> AsyncIterator[DocumentStore]:
        await _require_migrated(database)
        await _truncate(database)
        repository = PostgresKnowledgeBaseRepository(database)
        await repository.acreate(make_kb(make_draft().key.kb_id))
        yield PostgresDocumentStore(database)
        await _truncate(database)


async def _require_migrated(database: Database) -> None:
    async with database.session() as session:
        exists = (
            await session.execute(text("SELECT to_regclass('document_revisions')"))
        ).scalar_one()
    if exists is None:
        pytest.skip("document_revisions 表不存在，先跑 `uv run alembic upgrade head`")


async def _truncate(database: Database) -> None:
    await truncate_tables(database, "knowledge_bases")
