"""DocumentStore 值对象在 Port 边界拒绝模糊状态。"""

from __future__ import annotations

from typing import cast

import pytest

from comet_rag.ports.document_store import RevisionDraft, RevisionKey, StoredParent


@pytest.mark.parametrize("value_type", [RevisionDraft, StoredParent])
def test_metadata_values_take_nested_snapshots(
    value_type: type[RevisionDraft | StoredParent],
) -> None:
    metadata = {"nested": {"labels": ["original"]}}
    key = RevisionKey("kb", "source", "revision")
    if value_type is RevisionDraft:
        value = RevisionDraft(
            key=key,
            plan_sha256="a" * 64,
            parent_count=1,
            child_count=1,
            source="fixture.md",
            file_type="markdown",
            metadata=metadata,
        )
    else:
        value = StoredParent(
            key=key,
            parent_id="parent",
            ordinal=0,
            text="正文",
            start_char=0,
            end_char=2,
            metadata=metadata,
        )

    metadata["nested"]["labels"].append("input change")

    assert value.metadata == {"nested": {"labels": ["original"]}}
    nested = cast(dict[str, list[str]], value.metadata["nested"])
    nested["labels"].append("value change")
    assert metadata == {"nested": {"labels": ["original", "input change"]}}
    assert isinstance(nested, dict)
    assert isinstance(nested["labels"], list)


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
