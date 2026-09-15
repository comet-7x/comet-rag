"""规范文档结构事实的最小契约。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from comet_rag.ports import DocumentBlock, NormalizedDocument


def test_normalized_document_freezes_and_validates_block_order() -> None:
    metadata = {"source": "extractor"}
    blocks = [
        DocumentBlock("section", 0, 0, 3, heading_path=("A",), metadata=metadata),
        DocumentBlock("section", 1, 3, 6, heading_path=("B",)),
    ]

    document = NormalizedDocument(markdown="AAABBB", blocks=blocks)  # type: ignore[arg-type]
    metadata["source"] = "changed"
    blocks.clear()

    assert document.blocks[0].metadata == {"source": "extractor"}
    assert len(document.blocks) == 2
    with pytest.raises(TypeError):
        cast("dict[str, object]", document.blocks[0].metadata)["source"] = "x"
    with pytest.raises(FrozenInstanceError):
        document.blocks[0].end_char = 4  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "exception", "message"),
    [
        ({"kind": "", "ordinal": 0, "start_char": 0, "end_char": 1}, ValueError, "kind"),
        ({"kind": "page", "ordinal": True, "start_char": 0, "end_char": 1}, TypeError, "ordinal"),
        ({"kind": "page", "ordinal": -1, "start_char": 0, "end_char": 1}, ValueError, "ordinal"),
        ({"kind": "page", "ordinal": 0, "start_char": True, "end_char": 1}, TypeError, "字符位置"),
        ({"kind": "page", "ordinal": 0, "start_char": -1, "end_char": 1}, ValueError, "start_char"),
        ({"kind": "page", "ordinal": 0, "start_char": 1, "end_char": 1}, ValueError, "end_char"),
        ({"kind": "page", "ordinal": 0, "start_char": 0, "end_char": 1, "page_number": True}, TypeError, "page_number"),
        ({"kind": "page", "ordinal": 0, "start_char": 0, "end_char": 1, "page_number": 0}, ValueError, "page_number"),
        ({"kind": "section", "ordinal": 0, "start_char": 0, "end_char": 1, "heading_path": ["A"]}, TypeError, "heading_path"),
        ({"kind": "section", "ordinal": 0, "start_char": 0, "end_char": 1, "metadata": {"": 1}}, ValueError, "metadata"),
    ],
)
def test_document_block_rejects_invalid_state(
    kwargs: dict[str, object], exception: type[Exception], message: str
) -> None:
    with pytest.raises(exception, match=message):
        DocumentBlock(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("blocks", "message"),
    [
        ((DocumentBlock("section", 1, 0, 1),), "ordinal"),
        ((DocumentBlock("section", 0, 0, 4),), "超出"),
        (
            (
                DocumentBlock("section", 0, 0, 2),
                DocumentBlock("section", 1, 1, 3),
            ),
            "不能重叠",
        ),
    ],
)
def test_normalized_document_rejects_invalid_blocks(
    blocks: tuple[DocumentBlock, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        NormalizedDocument(markdown="abc", blocks=blocks)
