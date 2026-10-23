"""DocumentStore 共享契约；内存与 PostgreSQL 实现必须行为一致。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

from comet_rag.ports.document_store import (
    ActiveRevisionDeletion,
    DocumentStore,
    RevisionConflict,
    RevisionDraft,
    RevisionKey,
    RevisionNotReady,
    RevisionStatus,
    StaleRevision,
    StoredParent,
)

KB = "kb-1"
SOURCE = "source-1"


def make_draft(
    revision_id: str = "revision-1",
    *,
    kb_id: str = KB,
    source_id: str = SOURCE,
    parent_count: int = 1,
    child_count: int = 2,
    plan_sha256: str = "a" * 64,
    metadata: Mapping[str, object] | None = None,
) -> RevisionDraft:
    return RevisionDraft(
        key=RevisionKey(kb_id, source_id, revision_id),
        plan_sha256=plan_sha256,
        parent_count=parent_count,
        child_count=child_count,
        source="fixture.md",
        file_type="markdown",
        metadata=metadata or {"fixture": True},
        task_id="task-1",
    )


def make_parents(key: RevisionKey, count: int = 1) -> list[StoredParent]:
    ids = [f"{key.revision_id}-parent-{ordinal}" for ordinal in range(count)]
    return [
        StoredParent(
            key=key,
            parent_id=parent_id,
            ordinal=ordinal,
            text=f"父块 {ordinal}",
            start_char=ordinal * 10,
            end_char=ordinal * 10 + 4,
            previous_id=ids[ordinal - 1] if ordinal else None,
            next_id=ids[ordinal + 1] if ordinal + 1 < count else None,
            metadata={"ordinal": ordinal},
        )
        for ordinal, parent_id in enumerate(ids)
    ]


async def make_ready(
    store: DocumentStore, draft: RevisionDraft
) -> tuple[RevisionDraft, list[StoredParent]]:
    await store.reserve_revision(draft)
    parents = make_parents(draft.key, draft.parent_count)
    await store.put_parents(draft.key, parents)
    await store.mark_ready(draft.key)
    return draft, parents


class DocumentStoreContract:
    @pytest.fixture
    async def store(self) -> DocumentStore:  # pragma: no cover
        raise NotImplementedError("实现方必须提供 store fixture")

    async def test_reserve_allocates_monotonic_generation(
        self, store: DocumentStore
    ) -> None:
        first = await store.reserve_revision(make_draft("revision-1"))
        second = await store.reserve_revision(make_draft("revision-2"))

        assert (first.generation, second.generation) == (1, 2)
        assert first.status is RevisionStatus.STAGING

    async def test_reserve_same_plan_is_idempotent(self, store: DocumentStore) -> None:
        draft = make_draft()

        first = await store.reserve_revision(draft)
        second = await store.reserve_revision(draft)

        assert second == first

    async def test_reserve_same_id_with_changed_plan_is_rejected(
        self, store: DocumentStore
    ) -> None:
        await store.reserve_revision(make_draft())

        with pytest.raises(RevisionConflict):
            await store.reserve_revision(
                make_draft(plan_sha256="b" * 64, child_count=3)
            )

    async def test_concurrent_reservations_get_distinct_generations(
        self, store: DocumentStore
    ) -> None:
        first, second = await asyncio.gather(
            store.reserve_revision(make_draft("revision-1")),
            store.reserve_revision(make_draft("revision-2")),
        )

        assert {first.generation, second.generation} == {1, 2}

    async def test_concurrent_same_revision_is_one_reservation(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft()

        first, second = await asyncio.gather(
            store.reserve_revision(draft), store.reserve_revision(draft)
        )

        assert first == second
        assert first.generation == 1

    async def test_parent_batch_round_trips_and_replays(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft(parent_count=2)
        await store.reserve_revision(draft)
        parents = make_parents(draft.key, 2)

        await store.put_parents(draft.key, parents)
        await store.put_parents(draft.key, parents)
        loaded = await store.get_parents(KB, [parent.parent_id for parent in parents])

        assert set(loaded) == {parent.parent_id for parent in parents}
        assert loaded[parents[1].parent_id] == parents[1]

    async def test_parent_id_content_conflict_is_rejected(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft()
        await store.reserve_revision(draft)
        parent = make_parents(draft.key)[0]
        await store.put_parents(draft.key, [parent])

        changed = StoredParent(
            key=parent.key,
            parent_id=parent.parent_id,
            ordinal=parent.ordinal,
            text="被替换的正文",
            start_char=parent.start_char,
            end_char=parent.end_char,
        )
        with pytest.raises(RevisionConflict):
            await store.put_parents(draft.key, [changed])

    async def test_parent_from_another_revision_is_rejected(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft()
        await store.reserve_revision(draft)
        foreign = make_parents(RevisionKey(KB, SOURCE, "other"))[0]

        with pytest.raises(RevisionConflict):
            await store.put_parents(draft.key, [foreign])

    async def test_mark_ready_requires_exact_parent_chain(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft(parent_count=2)
        await store.reserve_revision(draft)
        parents = make_parents(draft.key, 2)
        broken = StoredParent(
            key=parents[1].key,
            parent_id=parents[1].parent_id,
            ordinal=parents[1].ordinal,
            text=parents[1].text,
            start_char=parents[1].start_char,
            end_char=parents[1].end_char,
            previous_id=None,
            next_id=None,
        )
        await store.put_parents(draft.key, [parents[0], broken])

        with pytest.raises(RevisionNotReady):
            await store.mark_ready(draft.key)

    async def test_ready_and_activation_are_idempotent(
        self, store: DocumentStore
    ) -> None:
        draft, _ = await make_ready(store, make_draft())
        assert (await store.mark_ready(draft.key)).status is RevisionStatus.READY

        first = await store.activate_revision(draft.key)
        second = await store.activate_revision(draft.key)

        assert first.active.status is RevisionStatus.ACTIVE
        assert second.already_active is True
        assert second.active == first.active

    async def test_activation_retires_previous_revision(
        self, store: DocumentStore
    ) -> None:
        first, _ = await make_ready(store, make_draft("revision-1"))
        await store.activate_revision(first.key)
        second, _ = await make_ready(store, make_draft("revision-2"))

        activated = await store.activate_revision(second.key)
        active = await store.get_active_revisions(KB, [SOURCE, "unknown"])
        cleanup = await store.list_cleanup_candidates(limit=10)

        assert activated.previous_revision_id == first.key.revision_id
        assert active == {SOURCE: second.key.revision_id, "unknown": None}
        assert first.key in cleanup

    async def test_older_generation_cannot_replace_newer_active(
        self, store: DocumentStore
    ) -> None:
        old, _ = await make_ready(store, make_draft("revision-old"))
        new, _ = await make_ready(store, make_draft("revision-new"))
        await store.activate_revision(new.key)

        with pytest.raises(StaleRevision):
            await store.activate_revision(old.key)

        assert await store.get_active_revisions(KB, [SOURCE]) == {
            SOURCE: new.key.revision_id
        }

    async def test_concurrent_activation_converges_on_newest_generation(
        self, store: DocumentStore
    ) -> None:
        old, _ = await make_ready(store, make_draft("revision-old"))
        new, _ = await make_ready(store, make_draft("revision-new"))

        outcomes = await asyncio.gather(
            store.activate_revision(old.key),
            store.activate_revision(new.key),
            return_exceptions=True,
        )

        assert not any(
            isinstance(outcome, BaseException)
            and not isinstance(outcome, StaleRevision)
            for outcome in outcomes
        )
        assert await store.get_active_revisions(KB, [SOURCE]) == {
            SOURCE: new.key.revision_id
        }

    async def test_parent_lookup_is_isolated_by_knowledge_base(
        self, store: DocumentStore
    ) -> None:
        draft, parents = await make_ready(store, make_draft())

        assert await store.get_parents("another-kb", [parents[0].parent_id]) == {}
        assert (await store.get_parents(KB, [parents[0].parent_id]))[
            parents[0].parent_id
        ].key == draft.key

    async def test_abandon_is_idempotent_and_cleanup_candidate(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft()
        await store.reserve_revision(draft)

        await store.abandon_revision(draft.key)
        await store.abandon_revision(draft.key)

        assert await store.list_cleanup_candidates(limit=1) == [draft.key]

    async def test_active_revision_cannot_be_abandoned_or_purged(
        self, store: DocumentStore
    ) -> None:
        draft, _ = await make_ready(store, make_draft())
        await store.activate_revision(draft.key)

        with pytest.raises(ActiveRevisionDeletion):
            await store.abandon_revision(draft.key)
        with pytest.raises(ActiveRevisionDeletion):
            await store.purge_inactive_revision(draft.key)

    async def test_purge_removes_parents_but_keeps_tombstone(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft()
        _, parents = await make_ready(store, draft)
        await store.abandon_revision(draft.key)

        await store.purge_inactive_revision(draft.key)
        await store.purge_inactive_revision(draft.key)
        replay = await store.reserve_revision(draft)

        assert await store.get_parents(KB, [parents[0].parent_id]) == {}
        assert replay.status is RevisionStatus.PURGED
        assert replay.generation == 1
        with pytest.raises(RevisionNotReady):
            await store.activate_revision(draft.key)

    async def test_cleanup_limit_must_be_positive(self, store: DocumentStore) -> None:
        with pytest.raises(ValueError):
            await store.list_cleanup_candidates(limit=0)

    async def test_abandon_ready_is_irreversible_before_vector_gc(
        self, store: DocumentStore
    ) -> None:
        draft, _ = await make_ready(store, make_draft())
        await store.abandon_revision(draft.key)
        with pytest.raises(RevisionNotReady):
            await store.activate_revision(draft.key)
        with pytest.raises(RevisionConflict):
            await store.mark_ready(draft.key)
        assert (await store.reserve_revision(draft)).status is RevisionStatus.ABANDONED
        assert await store.get_active_revisions(KB, [SOURCE]) == {SOURCE: None}

    async def test_cleanup_candidates_only_include_terminal_inactive_revisions(
        self, store: DocumentStore
    ) -> None:
        retired, _ = await make_ready(store, make_draft("retired"))
        active, _ = await make_ready(store, make_draft("active"))
        await store.activate_revision(retired.key)
        await store.activate_revision(active.key)
        await make_ready(store, make_draft("ready"))
        await store.reserve_revision(make_draft("staging"))
        abandoned, _ = await make_ready(store, make_draft("abandoned"))
        await store.abandon_revision(abandoned.key)
        purged, _ = await make_ready(store, make_draft("purged"))
        await store.abandon_revision(purged.key)
        await store.purge_inactive_revision(purged.key)
        assert await store.list_cleanup_candidates(limit=20) == [
            retired.key,
            abandoned.key,
        ]

    async def test_inputs_are_copied_at_value_boundary(
        self, store: DocumentStore
    ) -> None:
        metadata: dict[str, object] = {"version": 1}
        draft = make_draft(metadata=metadata)
        parent_metadata: dict[str, object] = {"page": 1}
        parent = StoredParent(
            key=draft.key,
            parent_id="parent-1",
            ordinal=0,
            text="正文",
            start_char=0,
            end_char=2,
            metadata=parent_metadata,
        )
        metadata["version"] = 2
        parent_metadata["page"] = 2
        await store.reserve_revision(draft)
        await store.put_parents(draft.key, [parent])

        loaded = (await store.get_parents(KB, [parent.parent_id]))[parent.parent_id]
        assert draft.metadata["version"] == 1
        assert loaded.metadata["page"] == 1

    async def test_reserved_draft_owns_nested_metadata(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft(metadata={"labels": ["original"]})
        original = await store.reserve_revision(draft)

        cast(list[str], draft.metadata["labels"]).append("changed")

        replay = await store.reserve_revision(
            make_draft(metadata={"labels": ["original"]})
        )
        assert replay == original
        with pytest.raises(RevisionConflict):
            await store.reserve_revision(draft)

    async def test_parent_write_owns_nested_metadata(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft()
        await store.reserve_revision(draft)
        parent = replace(
            make_parents(draft.key)[0], metadata={"nested": {"labels": ["original"]}}
        )
        await store.put_parents(draft.key, [parent])

        cast(dict[str, list[str]], parent.metadata["nested"])["labels"].append(
            "changed"
        )

        loaded = (await store.get_parents(KB, [parent.parent_id]))[parent.parent_id]
        assert loaded.metadata == {"nested": {"labels": ["original"]}}

    async def test_parent_reads_return_independent_nested_metadata(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft()
        await store.reserve_revision(draft)
        parent = replace(make_parents(draft.key)[0], metadata={"labels": ["original"]})
        await store.put_parents(draft.key, [parent])
        first = (await store.get_parents(KB, [parent.parent_id]))[parent.parent_id]
        second = (await store.get_parents(KB, [parent.parent_id]))[parent.parent_id]

        cast(list[str], first.metadata["labels"]).append("changed")

        latest = (await store.get_parents(KB, [parent.parent_id]))[parent.parent_id]
        assert second.metadata == {"labels": ["original"]}
        assert latest.metadata == {"labels": ["original"]}
        assert parent.metadata == {"labels": ["original"]}

    async def test_parent_records_do_not_share_nested_metadata(
        self, store: DocumentStore
    ) -> None:
        draft = make_draft(parent_count=2)
        await store.reserve_revision(draft)
        shared = {"labels": ["original"]}
        parents = [
            replace(parent, metadata=shared) for parent in make_parents(draft.key, 2)
        ]
        await store.put_parents(draft.key, parents)
        loaded = await store.get_parents(KB, [parent.parent_id for parent in parents])

        cast(list[str], loaded[parents[0].parent_id].metadata["labels"]).append(
            "changed"
        )

        assert loaded[parents[1].parent_id].metadata == {"labels": ["original"]}
        assert shared == {"labels": ["original"]}
