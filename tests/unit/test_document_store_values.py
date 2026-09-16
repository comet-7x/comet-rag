"""DocumentStore 值对象在 Port 边界拒绝模糊状态。"""

from __future__ import annotations

import pytest

from comet_rag.ports.document_store import RevisionDraft, RevisionKey, StoredParent


def test_revision_key_rejects_empty_identity() -> None:
    with pytest.raises(ValueError):
        RevisionKey("kb", "", "revision")
    with pytest.raises(ValueError):
        RevisionKey("kb", "s" * 65, "revision")


@pytest.mark.parametrize("count", [-1, True])
def test_revision_draft_rejects_invalid_counts(count: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        RevisionDraft(
            key=RevisionKey("kb", "source", "revision"),
            plan_sha256="a" * 64,
            parent_count=count,
            child_count=0,
            source="fixture.md",
            file_type="markdown",
        )


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 64, "z" * 64])
def test_revision_draft_requires_canonical_sha256(digest: str) -> None:
    with pytest.raises(ValueError):
        RevisionDraft(
            key=RevisionKey("kb", "source", "revision"),
            plan_sha256=digest,
            parent_count=0,
            child_count=1,
            source="fixture.md",
            file_type="markdown",
        )


def test_parent_rejects_invalid_span_and_self_link() -> None:
    key = RevisionKey("kb", "source", "revision")
    with pytest.raises(ValueError):
        StoredParent(
            key=key,
            parent_id="parent",
            ordinal=0,
            text="正文",
            start_char=2,
            end_char=2,
        )
    with pytest.raises(ValueError):
        StoredParent(
            key=key,
            parent_id="parent",
            ordinal=0,
            text="正文",
            start_char=0,
            end_char=2,
            next_id="parent",
        )
