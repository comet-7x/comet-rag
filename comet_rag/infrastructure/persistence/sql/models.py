"""ORM 模型。

**新增模型必须定义在本模块（或被本模块 import）**，否则
`alembic revision --autogenerate` 收集不到它，会误判为"这张表该删掉"。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from comet_rag.infrastructure.persistence.sql.base import Base, timestamp_column

#: JSONB 在 Postgres 上可索引可查询；其他方言退回普通 JSON，
#: 这样测试或小型部署换别的方言时模型不必改。
JsonType = JSON().with_variant(JSONB(), "postgresql")


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


class DocumentRow(Base):
    """每个知识库/source 的 generation 分配器与 active 指针。"""

    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.kb_id"],
            ondelete="CASCADE",
            name="fk_documents_kb_id_knowledge_bases",
        ),
        ForeignKeyConstraint(
            ["kb_id", "source_id", "active_revision_id"],
            [
                "document_revisions.kb_id",
                "document_revisions.source_id",
                "document_revisions.revision_id",
            ],
            name="fk_documents_active_revision",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "next_generation >= 0",
            name="documents_next_generation_nonnegative",
        ),
    )

    kb_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    active_revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=sql_text("0")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False)
    created_at: Mapped[datetime] = timestamp_column(nullable=False)
    updated_at: Mapped[datetime] = timestamp_column(nullable=False)


class DocumentRevisionRow(Base):
    """不可复用的 revision manifest；purged 行是 generation 防复活墓碑。"""

    __tablename__ = "document_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["kb_id", "source_id"],
            ["documents.kb_id", "documents.source_id"],
            ondelete="CASCADE",
            name="fk_document_revisions_document",
        ),
        UniqueConstraint(
            "kb_id",
            "source_id",
            "generation",
            name="uq_document_revisions_generation",
        ),
        UniqueConstraint(
            "kb_id",
            "source_id",
            "revision_id",
            name="uq_document_revisions_document_revision",
        ),
        CheckConstraint(
            "generation > 0",
            name="document_revisions_generation_positive",
        ),
        CheckConstraint(
            "parent_count >= 0 AND child_count >= 0",
            name="document_revisions_counts_nonnegative",
        ),
        CheckConstraint(
            "status IN ('staging', 'ready', 'active', 'retired', "
            "'abandoned', 'purged')",
            name="document_revisions_status",
        ),
        Index(
            "uq_document_revisions_active",
            "kb_id",
            "source_id",
            unique=True,
            postgresql_where=sql_text("status = 'active'"),
        ),
        Index(
            "ix_document_revisions_cleanup",
            "status",
            "updated_at",
            postgresql_where=sql_text("status IN ('retired', 'abandoned')"),
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    child_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replaces_revision_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column(nullable=False)
    updated_at: Mapped[datetime] = timestamp_column(nullable=False)
    ready_at: Mapped[datetime | None] = timestamp_column(nullable=True)
    activated_at: Mapped[datetime | None] = timestamp_column(nullable=True)
    purged_at: Mapped[datetime | None] = timestamp_column(nullable=True)


class DocumentParentRow(Base):
    """父块正文；其生命周期由 revision manifest 管理。"""

    __tablename__ = "document_parents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["kb_id", "source_id", "revision_id"],
            [
                "document_revisions.kb_id",
                "document_revisions.source_id",
                "document_revisions.revision_id",
            ],
            ondelete="CASCADE",
            name="fk_document_parents_revision",
        ),
        UniqueConstraint(
            "revision_id", "ordinal", name="uq_document_parents_revision_ordinal"
        ),
        CheckConstraint(
            "ordinal >= 0",
            name="document_parents_ordinal_nonnegative",
        ),
        CheckConstraint(
            "start_char >= 0 AND end_char > start_char",
            name="document_parents_span",
        ),
        Index("ix_document_parents_kb_parent", "kb_id", "parent_id"),
    )

    parent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kb_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False)
    created_at: Mapped[datetime] = timestamp_column(nullable=False)


class TaskRow(Base):
    """任务记录。

    **status 刻意用 varchar 而非 PG 原生 enum**（spec S2）：将来增删状态值
    （例如把确认门加回来）只是多一个字符串，不需要写 ALTER TYPE 迁移 ——
    而 PG 的 enum 改起来相当难受，尤其是删值。

    可查询的标量字段用真列（能建索引），结构化产物用 JSONB。
    不做"整个 Task 塞一个 JSONB"是因为那样 `WHERE status='failed'` 这类
    最常用的查询会退化成全表扫。
    """

    __tablename__ = "tasks"
    __table_args__ = (
        # 幂等键按 kind 分域：不同业务用同一个 key 是合理的
        UniqueConstraint("kind", "idempotency_key", name="uq_tasks_kind_idempotency"),
        # sweep_stale 扫的是"RUNNING 且心跳老"，这两列一起建索引
        Index("ix_tasks_status_heartbeat", "status", "heartbeat_at"),
        Index("ix_tasks_kind_created", "kind", "created_at"),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    request: Mapped[dict | None] = mapped_column(JsonType, nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stage_history: Mapped[list] = mapped_column(JsonType, nullable=False, default=list)

    context: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    result_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_stage: Mapped[str | None] = mapped_column(String(128), nullable=True)

    error: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 乐观锁。所有写入走 `UPDATE ... WHERE version = :expected`。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = timestamp_column(nullable=False, index=True)
    updated_at: Mapped[datetime] = timestamp_column(nullable=False)
    started_at: Mapped[datetime | None] = timestamp_column(nullable=True)
    finished_at: Mapped[datetime | None] = timestamp_column(nullable=True)
    heartbeat_at: Mapped[datetime | None] = timestamp_column(nullable=True)


class TaskEventRow(Base):
    """任务事件流。

    独立成表而非往 Task 上堆字段：事件是**只追加**的时间序列，
    和任务当前状态的读写模式完全不同（后者高频更新、前者只增不改）。
    """

    __tablename__ = "task_events"

    task_id: Mapped[str] = mapped_column(
        String(64),
        # 删任务时事件一并删掉，否则会留下查不到主体的孤儿事件
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = timestamp_column(nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)


__all__ = [
    "DocumentParentRow",
    "DocumentRevisionRow",
    "DocumentRow",
    "KnowledgeBaseRow",
    "TaskEventRow",
    "TaskRow",
]
