"""ORM 模型。

**新增模型必须定义在本模块（或被本模块 import）**，否则
`alembic revision --autogenerate` 收集不到它，会误判为"这张表该删掉"。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from comet_rag.infrastructure.database.base import Base, timestamp_column


class KnowledgeBaseRow(Base):
    """知识库元数据。

    `embedding_model` 与 `embedding_dim` 是这张表存在的**全部理由**（spec A12）：
    换了模型继续往同一个库里灌，新旧向量会混在不同语义空间里 —— 不报错、
    只是检索静默劣化，且事后无法分辨该重算哪些 chunk。维度不一致还能被
    向量库拦住，同维度的不同模型只有这张表能拦。
    """

    __tablename__ = "knowledge_bases"

    kb_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column(nullable=False, index=True)
    updated_at: Mapped[datetime] = timestamp_column(nullable=False)


__all__ = ["KnowledgeBaseRow"]
