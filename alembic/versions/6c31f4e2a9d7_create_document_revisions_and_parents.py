"""create document revisions and parents

Revision ID: 6c31f4e2a9d7
Revises: bf0412c94a7a
Create Date: 2026-09-16 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6c31f4e2a9d7"
down_revision: str | Sequence[str] | None = "bf0412c94a7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql"
    )


def upgrade() -> None:
    """Create the revision manifest and parent-document tables."""
    op.create_table(
        "documents",
        sa.Column("kb_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("active_revision_id", sa.String(length=64), nullable=True),
        sa.Column(
            "next_generation",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("file_type", sa.String(length=32), nullable=False),
        sa.Column("metadata", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "next_generation >= 0",
            name=op.f("ck_documents_documents_next_generation_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.kb_id"],
            name="fk_documents_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("kb_id", "source_id", name=op.f("pk_documents")),
    )
    op.create_table(
        "document_revisions",
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("plan_sha256", sa.String(length=64), nullable=False),
        sa.Column("parent_count", sa.Integer(), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("file_type", sa.String(length=32), nullable=False),
        sa.Column("metadata", _json_type(), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("replaces_revision_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "parent_count >= 0 AND child_count >= 0",
            name=op.f("ck_document_revisions_document_revisions_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "generation > 0",
            name=op.f("ck_document_revisions_document_revisions_generation_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('staging', 'ready', 'active', 'retired', "
            "'abandoned', 'purged')",
            name=op.f("ck_document_revisions_document_revisions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["kb_id", "source_id"],
            ["documents.kb_id", "documents.source_id"],
            name="fk_document_revisions_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("revision_id", name=op.f("pk_document_revisions")),
        sa.UniqueConstraint(
            "kb_id",
            "source_id",
            "generation",
            name="uq_document_revisions_generation",
        ),
        sa.UniqueConstraint(
            "kb_id",
            "source_id",
            "revision_id",
            name="uq_document_revisions_document_revision",
        ),
    )
    op.create_index(
        "uq_document_revisions_active",
        "document_revisions",
        ["kb_id", "source_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_document_revisions_cleanup",
        "document_revisions",
        ["status", "updated_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('retired', 'abandoned')"),
    )
    op.create_table(
        "document_parents",
        sa.Column("parent_id", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("previous_id", sa.String(length=64), nullable=True),
        sa.Column("next_id", sa.String(length=64), nullable=True),
        sa.Column("metadata", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name=op.f("ck_document_parents_document_parents_ordinal_nonnegative"),
        ),
        sa.CheckConstraint(
            "start_char >= 0 AND end_char > start_char",
            name=op.f("ck_document_parents_document_parents_span"),
        ),
        sa.ForeignKeyConstraint(
            ["kb_id", "source_id", "revision_id"],
            [
                "document_revisions.kb_id",
                "document_revisions.source_id",
                "document_revisions.revision_id",
            ],
            name="fk_document_parents_revision",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("parent_id", name=op.f("pk_document_parents")),
        sa.UniqueConstraint(
            "revision_id",
            "ordinal",
            name="uq_document_parents_revision_ordinal",
        ),
    )
    op.create_index(
        "ix_document_parents_kb_parent",
        "document_parents",
        ["kb_id", "parent_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_documents_active_revision",
        "documents",
        "document_revisions",
        ["kb_id", "source_id", "active_revision_id"],
        ["kb_id", "source_id", "revision_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    """Refuse destructive downgrade while revision data still exists."""
    bind = op.get_bind()
    checks = (
        ("document_parents", sa.text("SELECT count(*) FROM document_parents")),
        ("document_revisions", sa.text("SELECT count(*) FROM document_revisions")),
        ("documents", sa.text("SELECT count(*) FROM documents")),
    )
    for table, statement in checks:
        count = bind.execute(statement).scalar_one()
        if count:
            raise RuntimeError(
                f"拒绝删除非空表 {table}；先完成 active-only collection 回滚演练"
            )

    op.drop_constraint(
        "fk_documents_active_revision", "documents", type_="foreignkey"
    )
    op.drop_index("ix_document_parents_kb_parent", table_name="document_parents")
    op.drop_table("document_parents")
    op.drop_index("ix_document_revisions_cleanup", table_name="document_revisions")
    op.drop_index("uq_document_revisions_active", table_name="document_revisions")
    op.drop_table("document_revisions")
    op.drop_table("documents")
