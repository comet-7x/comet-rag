from __future__ import annotations

from dataclasses import replace

import pytest

from comet_rag.engines.chunkers import ChunkDraft, FixedSizeChunker
from comet_rag.engines.chunkers.strategies.errors import DocumentStructureError
from comet_rag.engines.indexing import (
    ChunkHierarchy,
    HierarchyBuilder,
    HierarchyChild,
    HierarchyParent,
)
from comet_rag.ports import DocumentBlock, NormalizedDocument


def test_parent_child_spans_and_overlap_are_global() -> None:
    document = NormalizedDocument(markdown="abcdabcdabcd")
    hierarchy = HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(4, 2)).split(
        document
    )
    assert [(p.text, p.start_char, p.end_char) for p in hierarchy.parents] == [
        ("abcdab", 0, 6),
        ("cdabcd", 6, 12),
    ]
    assert [
        (c.text, c.parent_ordinal, c.start_char, c.end_char) for c in hierarchy.children
    ] == [
        ("abcd", 0, 0, 4),
        ("cdab", 0, 2, 6),
        ("cdab", 1, 6, 10),
        ("abcd", 1, 8, 12),
    ]
    assert [c.ordinal for c in hierarchy.children] == [0, 1, 2, 3]


@pytest.mark.parametrize("kind", ["page", "section"])
def test_hard_structure_boundaries_and_facts(kind: str) -> None:
    document = NormalizedDocument(
        markdown="重复正文重复正文",
        blocks=tuple(
            DocumentBlock(
                kind=kind,
                ordinal=i,
                start_char=i * 4,
                end_char=(i + 1) * 4,
                page_number=i + 1 if kind == "page" else None,
                heading_path=(f"标题{i}",),
            )
            for i in range(2)
        ),
    )
    hierarchy = HierarchyBuilder(FixedSizeChunker(100), FixedSizeChunker(3)).split(
        document
    )
    assert [(p.start_char, p.end_char) for p in hierarchy.parents] == [(0, 4), (4, 8)]
    assert [c.start_char for c in hierarchy.children] == [0, 3, 4, 7]
    assert hierarchy.children[2].metadata["heading_path"] == ["标题1"]
    if kind == "page":
        assert hierarchy.children[2].metadata["page_number"] == 2
    with pytest.raises(TypeError):
        hierarchy.parents[0].metadata["forged"] = True  # type: ignore[index]


def test_empty_and_whitespace_have_no_hierarchy() -> None:
    builder = HierarchyBuilder(FixedSizeChunker(4), FixedSizeChunker(2))
    assert builder.split(NormalizedDocument(markdown=" ")).parents == ()
    assert builder.split(NormalizedDocument(markdown="")).children == ()


def test_parent_overlap_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="overlap"):
        HierarchyBuilder(FixedSizeChunker(4, 1), FixedSizeChunker(2))


class OverlappingParent:
    def split(self, text: str, /) -> list[ChunkDraft]:
        return [ChunkDraft(text[:4], 0, 0, 4), ChunkDraft(text[2:], 1, 2, len(text))]


def test_custom_parent_cannot_bypass_no_overlap_contract() -> None:
    with pytest.raises(DocumentStructureError, match="不重叠"):
        HierarchyBuilder(OverlappingParent(), FixedSizeChunker(2)).split(
            NormalizedDocument(markdown="abcdef")
        )


@pytest.mark.parametrize(
    "defect", ["span", "ordinal", "no_child", "parent_ref", "child_text"]
)
def test_hierarchy_rejects_invalid_relations(defect: str) -> None:
    parent = HierarchyParent(0, "abcd", 4, 8, {})
    child = HierarchyChild(0, 0, "abcd", 4, 8, {})
    children = (child,)
    if defect == "span":
        child = replace(child, start_char=3)
    elif defect == "ordinal":
        child = replace(child, ordinal=1)
    elif defect == "parent_ref":
        child = replace(child, parent_ordinal=1)
    elif defect == "child_text":
        child = replace(child, text="zzzz")
    else:
        children = ()
    with pytest.raises(DocumentStructureError):
        ChunkHierarchy((parent,), (child,) if children else ())


def test_parent_span_requires_exact_body_length() -> None:
    with pytest.raises(DocumentStructureError):
        HierarchyParent(0, "正文", 0, 9, {})


@pytest.mark.parametrize("lane", ["parent", "child"])
@pytest.mark.parametrize("spans", [[], [(3, 9)], [(0, 3), (6, 9)], [(0, 6)]])
def test_hierarchy_cannot_silently_drop_source_content(lane, spans) -> None:
    class FaultyChunker:
        def split(self, text: str, /) -> list[ChunkDraft]:
            return [
                ChunkDraft(text[start:end], i, start, end)
                for i, (start, end) in enumerate(spans)
            ]

    good = FixedSizeChunker(9)
    builder = HierarchyBuilder(
        FaultyChunker() if lane == "parent" else good,
        FaultyChunker() if lane == "child" else good,
    )
    with pytest.raises(DocumentStructureError):
        builder.split(NormalizedDocument("AAABBBCCC"))
