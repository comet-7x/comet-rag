"""DocumentStore 的进程内实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace

from comet_rag.core.time import Time
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


@dataclass(slots=True)
class _DocumentState:
    next_generation: int = 0
    active_revision_id: str | None = None


class InMemoryDocumentStore(DocumentStore):
    """零中间件实现；锁内行为与 PostgreSQL 行锁语义一致。"""

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], _DocumentState] = {}
        self._revisions: dict[RevisionKey, StoredRevision] = {}
        self._drafts: dict[RevisionKey, RevisionDraft] = {}
        self._revision_ids: dict[str, RevisionKey] = {}
        self._parents: dict[str, StoredParent] = {}
        self._lock = asyncio.Lock()

    async def reserve_revision(self, draft: RevisionDraft) -> StoredRevision:
        async with self._lock:
            owner = self._revision_ids.get(draft.key.revision_id)
            if owner is not None and owner != draft.key:
                raise RevisionConflict(draft.key, "revision_id 已被其他文档使用")

            current = self._revisions.get(draft.key)
            if current is not None:
                if self._drafts[draft.key] != draft:
                    raise RevisionConflict(draft.key, "相同 revision_id 的计划发生变化")
                return current

            # 复制失败不能占用 revision_id 或消耗 generation。
            stored_draft = replace(draft)
            document_key = (draft.key.kb_id, draft.key.source_id)
            document = self._documents.setdefault(document_key, _DocumentState())
            document.next_generation += 1
            now = Time.utcnow()
            revision = StoredRevision(
                key=draft.key,
                generation=document.next_generation,
                status=RevisionStatus.STAGING,
                plan_sha256=draft.plan_sha256,
                parent_count=draft.parent_count,
                child_count=draft.child_count,
                task_id=draft.task_id,
                replaces_revision_id=None,
                created_at=now,
                updated_at=now,
            )
            self._revisions[draft.key] = revision
            self._drafts[draft.key] = stored_draft
            self._revision_ids[draft.key.revision_id] = draft.key
            return revision

    async def put_parents(
        self, revision: RevisionKey, parents: Sequence[StoredParent]
    ) -> None:
        batch = tuple(parents)
        async with self._lock:
            row = self._require_revision(revision)
            seen_ids: set[str] = set()
            seen_ordinals: set[int] = set()
            for parent in batch:
                if parent.key != revision:
                    raise RevisionConflict(revision, "父块属于另一个 revision")
                if parent.parent_id in seen_ids or parent.ordinal in seen_ordinals:
                    raise RevisionConflict(revision, "父块批次包含重复 ID 或 ordinal")
                seen_ids.add(parent.parent_id)
                seen_ordinals.add(parent.ordinal)

                existing = self._parents.get(parent.parent_id)
                if existing is not None:
                    if existing != parent:
                        raise RevisionConflict(
                            revision, "相同 parent_id 的内容发生变化"
                        )
                    continue
                ordinal_owner = next(
                    (
                        stored
                        for stored in self._parents.values()
                        if stored.key == revision and stored.ordinal == parent.ordinal
                    ),
                    None,
                )
                if ordinal_owner is not None:
                    raise RevisionConflict(revision, "相同 ordinal 已属于另一个父块")
                if row.status is not RevisionStatus.STAGING:
                    raise RevisionConflict(revision, "只有 staging revision 可新增父块")

            current_count = sum(
                parent.key == revision for parent in self._parents.values()
            )
            new_count = sum(parent.parent_id not in self._parents for parent in batch)
            if current_count + new_count > row.parent_count:
                raise RevisionConflict(revision, "父块数量超过 revision 计划")
            # 全部快照成功后才能发布，避免后续父块的复制失败留下半批数据。
            batch = tuple(replace(parent) for parent in batch)
            for parent in batch:
                self._parents.setdefault(parent.parent_id, parent)

    async def mark_ready(self, revision: RevisionKey) -> StoredRevision:
        async with self._lock:
            row = self._require_revision(revision)
            if row.status in (RevisionStatus.READY, RevisionStatus.ACTIVE):
                return row
            if row.status is not RevisionStatus.STAGING:
                raise RevisionConflict(revision, f"不能从 {row.status} 标记 ready")
            parents = [
                parent for parent in self._parents.values() if parent.key == revision
            ]
            assert_complete_parent_chain(revision, parents, row.parent_count)
            now = Time.utcnow()
            ready = replace(
                row,
                status=RevisionStatus.READY,
                ready_at=now,
                updated_at=now,
            )
            self._revisions[revision] = ready
            return ready

    async def activate_revision(self, revision: RevisionKey) -> Activation:
        async with self._lock:
            target = self._require_revision(revision)
            document = self._documents[(revision.kb_id, revision.source_id)]
            if (
                target.status is RevisionStatus.ACTIVE
                and document.active_revision_id == revision.revision_id
            ):
                return Activation(
                    active=target,
                    previous_revision_id=target.replaces_revision_id,
                    already_active=True,
                )
            if target.status is not RevisionStatus.READY:
                raise RevisionNotReady(revision, f"当前状态为 {target.status}")

            previous_id = document.active_revision_id
            if previous_id is not None:
                previous_key = self._revision_ids[previous_id]
                previous = self._revisions[previous_key]
                if previous.generation >= target.generation:
                    raise StaleRevision(
                        revision,
                        generation=target.generation,
                        active_generation=previous.generation,
                    )
                now = Time.utcnow()
                self._revisions[previous_key] = replace(
                    previous,
                    status=RevisionStatus.RETIRED,
                    updated_at=now,
                )
            else:
                now = Time.utcnow()

            active = replace(
                target,
                status=RevisionStatus.ACTIVE,
                replaces_revision_id=previous_id,
                activated_at=now,
                updated_at=now,
            )
            self._revisions[revision] = active
            document.active_revision_id = revision.revision_id
            return Activation(active=active, previous_revision_id=previous_id)

    async def get_active_revisions(
        self, kb_id: str, source_ids: Sequence[str]
    ) -> dict[str, str | None]:
        async with self._lock:
            return {
                source_id: (
                    self._documents.get((kb_id, source_id), _DocumentState())
                ).active_revision_id
                for source_id in dict.fromkeys(source_ids)
            }

    async def get_parents(
        self, kb_id: str, parent_ids: Sequence[str]
    ) -> dict[str, StoredParent]:
        async with self._lock:
            return {
                parent_id: replace(parent)
                for parent_id in dict.fromkeys(parent_ids)
                if (parent := self._parents.get(parent_id)) is not None
                and parent.key.kb_id == kb_id
            }

    async def abandon_revision(self, revision: RevisionKey) -> None:
        async with self._lock:
            row = self._require_revision(revision)
            if row.status is RevisionStatus.ACTIVE:
                raise ActiveRevisionDeletion(revision)
            if row.status in (
                RevisionStatus.ABANDONED,
                RevisionStatus.RETIRED,
                RevisionStatus.PURGED,
            ):
                return
            now = Time.utcnow()
            self._revisions[revision] = replace(
                row,
                status=RevisionStatus.ABANDONED,
                updated_at=now,
            )

    async def list_cleanup_candidates(self, *, limit: int) -> list[RevisionKey]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        async with self._lock:
            rows = sorted(
                (
                    row
                    for row in self._revisions.values()
                    if row.status in (RevisionStatus.RETIRED, RevisionStatus.ABANDONED)
                ),
                key=lambda row: (row.updated_at, row.generation),
            )
            return [row.key for row in rows[:limit]]

    async def purge_inactive_revision(self, revision: RevisionKey) -> None:
        async with self._lock:
            row = self._require_revision(revision)
            if row.status is RevisionStatus.ACTIVE:
                raise ActiveRevisionDeletion(revision)
            if row.status is RevisionStatus.PURGED:
                return
            if row.status not in (
                RevisionStatus.RETIRED,
                RevisionStatus.ABANDONED,
            ):
                raise RevisionConflict(revision, f"不能从 {row.status} 清理")
            self._parents = {
                parent_id: parent
                for parent_id, parent in self._parents.items()
                if parent.key != revision
            }
            now = Time.utcnow()
            self._revisions[revision] = replace(
                row,
                status=RevisionStatus.PURGED,
                purged_at=now,
                updated_at=now,
            )

    def _require_revision(self, key: RevisionKey) -> StoredRevision:
        try:
            return self._revisions[key]
        except KeyError as exc:
            raise RevisionNotFound(key) from exc


__all__ = ["InMemoryDocumentStore"]
