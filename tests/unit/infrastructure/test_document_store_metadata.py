from __future__ import annotations

from dataclasses import replace
from typing import NoReturn, cast

import pytest

from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.ports.document_store import RevisionNotFound
from tests.contracts.document_store import KB, make_draft, make_parents


class _CopyFailure:
    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        raise RuntimeError("injected metadata copy failure")


@pytest.mark.parametrize("existing_revision", [False, True])
async def test_draft_copy_failure_does_not_reserve_or_consume_generation(
    existing_revision: bool,
) -> None:
    store = InMemoryDocumentStore()
    if existing_revision:
        await store.reserve_revision(make_draft("existing"))
    draft = make_draft("failed", metadata={"values": []})
    values = cast(list[object], draft.metadata["values"])
    values.append(_CopyFailure())

    with pytest.raises(RuntimeError, match="injected metadata copy failure"):
        await store.reserve_revision(draft)

    with pytest.raises(RevisionNotFound):
        await store.mark_ready(draft.key)
    next_revision = await store.reserve_revision(make_draft("next"))
    assert next_revision.generation == (2 if existing_revision else 1)
    values.clear()
    retried = await store.reserve_revision(draft)
    assert retried.generation == next_revision.generation + 1


async def test_parent_copy_failure_publishes_no_partial_batch() -> None:
    store = InMemoryDocumentStore()
    draft = make_draft(parent_count=3)
    await store.reserve_revision(draft)
    parents = [
        replace(parent, metadata={"values": []})
        for parent in make_parents(draft.key, 3)
    ]
    await store.put_parents(draft.key, parents[:1])
    values = cast(list[object], parents[2].metadata["values"])
    values.append(_CopyFailure())

    with pytest.raises(RuntimeError, match="injected metadata copy failure"):
        await store.put_parents(draft.key, parents[1:])

    loaded = await store.get_parents(KB, [parent.parent_id for parent in parents])
    assert list(loaded) == [parents[0].parent_id]
    assert loaded[parents[0].parent_id] == parents[0]
    values.clear()
    await store.put_parents(draft.key, parents[1:])
    assert (
        len(await store.get_parents(KB, [parent.parent_id for parent in parents])) == 3
    )
    await store.mark_ready(draft.key)
