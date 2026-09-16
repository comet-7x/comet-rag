"""两个 DocumentStore 实现必须共享的结构校验。"""

from __future__ import annotations

from comet_rag.ports.document_store import (
    RevisionKey,
    RevisionNotReady,
    StoredParent,
)


def assert_complete_parent_chain(
    revision: RevisionKey, parents: list[StoredParent], expected_count: int
) -> None:
    if len(parents) != expected_count:
        raise RevisionNotReady(
            revision,
            f"父块数量应为 {expected_count}，实际为 {len(parents)}",
        )
    parents.sort(key=lambda parent: parent.ordinal)
    for ordinal, parent in enumerate(parents):
        if parent.ordinal != ordinal:
            raise RevisionNotReady(revision, "父块 ordinal 必须从 0 严格连续")
        expected_previous = parents[ordinal - 1].parent_id if ordinal else None
        expected_next = (
            parents[ordinal + 1].parent_id if ordinal + 1 < len(parents) else None
        )
        if (
            parent.previous_id != expected_previous
            or parent.next_id != expected_next
        ):
            raise RevisionNotReady(revision, "父块相邻关系与 ordinal 不一致")


__all__ = ["assert_complete_parent_chain"]
