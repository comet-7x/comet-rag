"""`KnowledgeBaseRepository` 的 PostgreSQL 实现。

只做存取，不含业务规则 —— 模型一致性校验在领域对象
（`KnowledgeBase.assert_model_matches`）里，两种实现共用同一份规则。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from comet_rag.infrastructure.database.models import KnowledgeBaseRow
from comet_rag.infrastructure.database.session import Database
from comet_rag.infrastructure.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseExists,
    KnowledgeBaseRepository,
)


def _to_domain(row: KnowledgeBaseRow) -> KnowledgeBase:
    return KnowledgeBase(
        kb_id=row.kb_id,
        name=row.name,
        embedding_model=row.embedding_model,
        embedding_dim=row.embedding_dim,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgresKnowledgeBaseRepository(KnowledgeBaseRepository):
    def __init__(self, database: Database) -> None:
        self._db = database

    async def acreate(self, kb: KnowledgeBase) -> KnowledgeBase:
        try:
            async with self._db.transaction() as session:
                session.add(
                    KnowledgeBaseRow(
                        kb_id=kb.kb_id,
                        name=kb.name,
                        embedding_model=kb.embedding_model,
                        embedding_dim=kb.embedding_dim,
                        description=kb.description,
                        created_at=kb.created_at,
                        updated_at=kb.updated_at,
                    )
                )
        except IntegrityError as exc:
            # 靠主键冲突而非"先查后插"：后者在并发下有竞态窗口，
            # 两个请求可能同时查到"不存在"然后都去插。
            raise KnowledgeBaseExists(kb.kb_id) from exc
        return kb

    async def aget(self, kb_id: str) -> KnowledgeBase | None:
        async with self._db.session() as session:
            row = await session.get(KnowledgeBaseRow, kb_id)
            return _to_domain(row) if row else None

    async def alist(self, *, limit: int = 50, offset: int = 0) -> list[KnowledgeBase]:
        async with self._db.session() as session:
            result = await session.execute(
                select(KnowledgeBaseRow)
                .order_by(KnowledgeBaseRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_to_domain(row) for row in result.scalars()]

    async def adelete(self, kb_id: str) -> bool:
        async with self._db.transaction() as session:
            result = await session.execute(
                delete(KnowledgeBaseRow).where(KnowledgeBaseRow.kb_id == kb_id)
            )
            return bool(result.rowcount)


__all__ = ["PostgresKnowledgeBaseRepository"]
