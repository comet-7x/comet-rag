"""DocumentStore 的 PostgreSQL 实现。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from comet_rag.core.time import Time
from comet_rag.infrastructure.persistence.sql.models import (
    DocumentParentRow,
    DocumentRevisionRow,
    DocumentRow,
)
from comet_rag.infrastructure.persistence.sql.session import Database
from comet_rag.ports.document_store import (
    Activation,
    ActiveRevisionDeletion,
    DocumentStore,
    RevisionConflict,
    RevisionDraft,
    RevisionKey,
    RevisionNotFound,
    RevisionNotReady,
    RevisionStatus,
    StaleRevision,
    StoredParent,
    StoredRevision,
)

from ._validation import assert_complete_parent_chain


def _key(row: DocumentRevisionRow) -> RevisionKey:
    return RevisionKey(
        kb_id=row.kb_id,
        source_id=row.source_id,
        revision_id=row.revision_id,
    )


def _to_revision(row: DocumentRevisionRow) -> StoredRevision:
    return StoredRevision(
        key=_key(row),
        generation=row.generation,
        status=RevisionStatus(row.status),
        plan_sha256=row.plan_sha256,
        parent_count=row.parent_count,
        child_count=row.child_count,
        task_id=row.task_id,
        replaces_revision_id=row.replaces_revision_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ready_at=row.ready_at,
        activated_at=row.activated_at,
        purged_at=row.purged_at,
    )


def _to_parent(row: DocumentParentRow) -> StoredParent:
    return StoredParent(
        key=RevisionKey(
            kb_id=row.kb_id,
            source_id=row.source_id,
            revision_id=row.revision_id,
        ),
        parent_id=row.parent_id,
        ordinal=row.ordinal,
        text=row.text,
        start_char=row.start_char,
        end_char=row.end_char,
        previous_id=row.previous_id,
        next_id=row.next_id,
        metadata=row.metadata_,
    )


def _draft_matches(row: DocumentRevisionRow, draft: RevisionDraft) -> bool:
    return (
        _key(row) == draft.key
        and row.plan_sha256 == draft.plan_sha256
        and row.parent_count == draft.parent_count
        and row.child_count == draft.child_count
        and row.source == draft.source
        and row.file_type == draft.file_type
        and row.metadata_ == dict(draft.metadata)
        and row.task_id == draft.task_id
    )


def _parent_matches(row: DocumentParentRow, parent: StoredParent) -> bool:
    return (
        row.revision_id == parent.key.revision_id
        and row.kb_id == parent.key.kb_id
        and row.source_id == parent.key.source_id
        and row.parent_id == parent.parent_id
        and row.ordinal == parent.ordinal
        and row.text == parent.text
        and row.start_char == parent.start_char
        and row.end_char == parent.end_char
        and row.previous_id == parent.previous_id
        and row.next_id == parent.next_id
        and row.metadata_ == dict(parent.metadata)
    )


async def _revision_row(
    session: AsyncSession, key: RevisionKey, *, for_update: bool = False
) -> DocumentRevisionRow:
    statement = select(DocumentRevisionRow).where(
        DocumentRevisionRow.revision_id == key.revision_id,
        DocumentRevisionRow.kb_id == key.kb_id,
        DocumentRevisionRow.source_id == key.source_id,
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).scalar_one_or_none()
    if row is None:
        raise RevisionNotFound(key)
    return row


def _require_positive_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit 必须是正整数")


class PostgresDocumentStore(DocumentStore):
    """依赖注入的 Database 由组合根持有，本适配器不会越权关闭它。"""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def reserve_revision(self, draft: RevisionDraft) -> StoredRevision:
        try:
            async with self._db.transaction() as session:
                now = Time.utcnow()
                await session.execute(
                    pg_insert(DocumentRow)
                    .values(
                        kb_id=draft.key.kb_id,
                        source_id=draft.key.source_id,
                        active_revision_id=None,
                        next_generation=0,
                        source=draft.source,
                        file_type=draft.file_type,
                        metadata_=dict(draft.metadata),
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[DocumentRow.kb_id, DocumentRow.source_id]
                    )
                )
                document = (
                    await session.execute(
                        select(DocumentRow)
                        .where(
                            DocumentRow.kb_id == draft.key.kb_id,
                            DocumentRow.source_id == draft.key.source_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one()

                existing = (
                    await session.execute(
                        select(DocumentRevisionRow).where(
                            DocumentRevisionRow.revision_id == draft.key.revision_id
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if not _draft_matches(existing, draft):
                        raise RevisionConflict(
                            draft.key, "相同 revision_id 的计划发生变化"
                        )
                    return _to_revision(existing)

                document.next_generation += 1
                document.updated_at = now
                row = DocumentRevisionRow(
                    revision_id=draft.key.revision_id,
                    kb_id=draft.key.kb_id,
                    source_id=draft.key.source_id,
                    generation=document.next_generation,
                    status=RevisionStatus.STAGING.value,
                    plan_sha256=draft.plan_sha256,
                    parent_count=draft.parent_count,
                    child_count=draft.child_count,
                    source=draft.source,
                    file_type=draft.file_type,
                    metadata_=dict(draft.metadata),
                    task_id=draft.task_id,
                    replaces_revision_id=None,
                    created_at=now,
                    updated_at=now,
                    ready_at=None,
                    activated_at=None,
                    purged_at=None,
                )
                session.add(row)
                await session.flush()
                return _to_revision(row)
        except IntegrityError as exc:
            raise RevisionConflict(draft.key, "数据库唯一键或外键冲突") from exc

    async def put_parents(
        self, revision: RevisionKey, parents: Sequence[StoredParent]
    ) -> None:
        batch = tuple(parents)
        try:
            async with self._db.transaction() as session:
                row = await _revision_row(session, revision, for_update=True)
                seen_ids: set[str] = set()
                seen_ordinals: set[int] = set()
                for parent in batch:
                    if parent.key != revision:
                        raise RevisionConflict(revision, "父块属于另一个 revision")
                    if parent.parent_id in seen_ids or parent.ordinal in seen_ordinals:
                        raise RevisionConflict(
                            revision, "父块批次包含重复 ID 或 ordinal"
                        )
                    seen_ids.add(parent.parent_id)
                    seen_ordinals.add(parent.ordinal)

                existing_rows = []
                if batch:
                    existing_rows = list(
                        (
                            await session.execute(
                                select(DocumentParentRow).where(
                                    or_(
                                        DocumentParentRow.parent_id.in_(seen_ids),
                                        (
                                            DocumentParentRow.revision_id
                                            == revision.revision_id
                                        )
                                        & DocumentParentRow.ordinal.in_(seen_ordinals),
                                    )
                                )
                            )
                        ).scalars()
                    )
                by_id = {parent.parent_id: parent for parent in existing_rows}
                by_ordinal = {
                    parent.ordinal: parent
                    for parent in existing_rows
                    if parent.revision_id == revision.revision_id
                }
                new_parents: list[StoredParent] = []
                for parent in batch:
                    existing = by_id.get(parent.parent_id)
                    if existing is not None:
                        if not _parent_matches(existing, parent):
                            raise RevisionConflict(
                                revision, "相同 parent_id 的内容发生变化"
                            )
                        continue
                    if parent.ordinal in by_ordinal:
                        raise RevisionConflict(
                            revision, "相同 ordinal 已属于另一个父块"
                        )
                    if RevisionStatus(row.status) is not RevisionStatus.STAGING:
                        raise RevisionConflict(
                            revision, "只有 staging revision 可新增父块"
                        )
                    new_parents.append(parent)

                current_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(DocumentParentRow)
                        .where(DocumentParentRow.revision_id == revision.revision_id)
                    )
                ).scalar_one()
                if current_count + len(new_parents) > row.parent_count:
                    raise RevisionConflict(revision, "父块数量超过 revision 计划")

                now = Time.utcnow()
                for parent in new_parents:
                    session.add(
                        DocumentParentRow(
                            parent_id=parent.parent_id,
                            revision_id=parent.key.revision_id,
                            kb_id=parent.key.kb_id,
                            source_id=parent.key.source_id,
                            ordinal=parent.ordinal,
                            text=parent.text,
                            start_char=parent.start_char,
                            end_char=parent.end_char,
                            previous_id=parent.previous_id,
                            next_id=parent.next_id,
                            metadata_=dict(parent.metadata),
                            created_at=now,
                        )
                    )
                await session.flush()
        except IntegrityError as exc:
            raise RevisionConflict(revision, "数据库唯一键或外键冲突") from exc

    async def mark_ready(self, revision: RevisionKey) -> StoredRevision:
        async with self._db.transaction() as session:
            row = await _revision_row(session, revision, for_update=True)
            status = RevisionStatus(row.status)
            if status in (RevisionStatus.READY, RevisionStatus.ACTIVE):
                return _to_revision(row)
            if status is not RevisionStatus.STAGING:
                raise RevisionConflict(revision, f"不能从 {status} 标记 ready")
            parent_rows = list(
                (
                    await session.execute(
                        select(DocumentParentRow).where(
                            DocumentParentRow.revision_id == revision.revision_id
                        )
                    )
                ).scalars()
            )
            assert_complete_parent_chain(
                revision,
                [_to_parent(parent) for parent in parent_rows],
                row.parent_count,
            )
            now = Time.utcnow()
            row.status = RevisionStatus.READY.value
            row.ready_at = now
            row.updated_at = now
            await session.flush()
            return _to_revision(row)

    async def activate_revision(self, revision: RevisionKey) -> Activation:
        async with self._db.transaction() as session:
            document = (
                await session.execute(
                    select(DocumentRow)
                    .where(
                        DocumentRow.kb_id == revision.kb_id,
                        DocumentRow.source_id == revision.source_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if document is None:
                raise RevisionNotFound(revision)
            target = await _revision_row(session, revision, for_update=True)
            status = RevisionStatus(target.status)
            if (
                status is RevisionStatus.ACTIVE
                and document.active_revision_id == revision.revision_id
            ):
                return Activation(
                    active=_to_revision(target),
                    previous_revision_id=target.replaces_revision_id,
                    already_active=True,
                )
            if status is not RevisionStatus.READY:
                raise RevisionNotReady(revision, f"当前状态为 {status}")

            previous_id = document.active_revision_id
            previous: DocumentRevisionRow | None = None
            if previous_id is not None:
                previous = (
                    await session.execute(
                        select(DocumentRevisionRow)
                        .where(DocumentRevisionRow.revision_id == previous_id)
                        .with_for_update()
                    )
                ).scalar_one()
                if previous.generation >= target.generation:
                    raise StaleRevision(
                        revision,
                        generation=target.generation,
                        active_generation=previous.generation,
                    )

            now = Time.utcnow()
            if previous is not None:
                previous.status = RevisionStatus.RETIRED.value
                previous.updated_at = now
                # active partial unique index 不是 deferrable；先让旧行退出索引，
                # 再激活新行。两次 flush 仍在同一个事务里，对外没有空窗。
                await session.flush([previous])
            target.status = RevisionStatus.ACTIVE.value
            target.replaces_revision_id = previous_id
            target.activated_at = now
            target.updated_at = now
            document.active_revision_id = revision.revision_id
            document.source = target.source
            document.file_type = target.file_type
            document.metadata_ = dict(target.metadata_)
            document.updated_at = now
            await session.flush()
            return Activation(
                active=_to_revision(target), previous_revision_id=previous_id
            )

    async def get_active_revisions(
        self, kb_id: str, source_ids: Sequence[str]
    ) -> dict[str, str | None]:
        sources = tuple(dict.fromkeys(source_ids))
        result = dict.fromkeys(sources)
        if not sources:
            return result
        async with self._db.session() as session:
            rows = await session.execute(
                select(DocumentRow.source_id, DocumentRow.active_revision_id).where(
                    DocumentRow.kb_id == kb_id,
                    DocumentRow.source_id.in_(sources),
                )
            )
            for source_id, active_revision_id in rows.tuples():
                result[source_id] = active_revision_id
            return result

    async def get_parents(
        self, kb_id: str, parent_ids: Sequence[str]
    ) -> dict[str, StoredParent]:
        ids = tuple(dict.fromkeys(parent_ids))
        if not ids:
            return {}
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(DocumentParentRow).where(
                        DocumentParentRow.kb_id == kb_id,
                        DocumentParentRow.parent_id.in_(ids),
                    )
                )
            ).scalars()
            return {row.parent_id: _to_parent(row) for row in rows}

    async def abandon_revision(self, revision: RevisionKey) -> None:
        async with self._db.transaction() as session:
            row = await _revision_row(session, revision, for_update=True)
            status = RevisionStatus(row.status)
            if status is RevisionStatus.ACTIVE:
                raise ActiveRevisionDeletion(revision)
            if status in (
                RevisionStatus.ABANDONED,
                RevisionStatus.RETIRED,
                RevisionStatus.PURGED,
            ):
                return
            row.status = RevisionStatus.ABANDONED.value
            row.updated_at = Time.utcnow()

    async def list_cleanup_candidates(self, *, limit: int) -> list[RevisionKey]:
        _require_positive_limit(limit)
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(DocumentRevisionRow)
                    .where(
                        DocumentRevisionRow.status.in_(
                            (
                                RevisionStatus.RETIRED.value,
                                RevisionStatus.ABANDONED.value,
                            )
                        )
                    )
                    .order_by(
                        DocumentRevisionRow.updated_at,
                        DocumentRevisionRow.generation,
                    )
                    .limit(limit)
                )
            ).scalars()
            return [_key(row) for row in rows]

    async def purge_inactive_revision(self, revision: RevisionKey) -> None:
        async with self._db.transaction() as session:
            row = await _revision_row(session, revision, for_update=True)
            status = RevisionStatus(row.status)
            if status is RevisionStatus.ACTIVE:
                raise ActiveRevisionDeletion(revision)
            if status is RevisionStatus.PURGED:
                return
            if status not in (RevisionStatus.RETIRED, RevisionStatus.ABANDONED):
                raise RevisionConflict(revision, f"不能从 {status} 清理")
            await session.execute(
                delete(DocumentParentRow).where(
                    DocumentParentRow.revision_id == revision.revision_id
                )
            )
            now = Time.utcnow()
            row.status = RevisionStatus.PURGED.value
            row.purged_at = now
            row.updated_at = now


__all__ = ["PostgresDocumentStore"]
