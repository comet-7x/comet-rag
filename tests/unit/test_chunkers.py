from __future__ import annotations

import subprocess  # nosec B404
import sys
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any

import pytest

from comet_rag.chunkers import (
    Chunk,
    DocumentChunker,
    RecursiveTextChunker,
    SourceRef,
    TextSpan,
)
from comet_rag.content import MediaRef
from comet_rag.documents.projection import project_document
from comet_rag.documents.schemas import (
    Asset,
    Block,
    FormulaContent,
    InlineCode,
    InlineFormula,
    ParsedDocument,
    ParsingInfo,
    RichText,
    SourceInfo,
    TableCell,
    TableContent,
    TextRun,
)


def paragraph(block_id: str, text: str, parent: str | None = None) -> Block:
    return Block(
        block_id,
        "paragraph",
        rich_text=RichText((TextRun(text),)),
        parent_block_id=parent,
    )


def document(*blocks: Block, assets: tuple[Asset, ...] = ()) -> ParsedDocument:
    return ParsedDocument(
        SourceInfo("source", "a" * 64),
        ParsingInfo("test"),
        blocks,
        assets=assets,
    )


def media_asset() -> Asset:
    return Asset(
        "pixels",
        "b" * 64,
        MediaRef(path=Path("/retained/image.png"), media_type="image/png"),
    )


def ranges(spans: list[TextSpan]) -> list[tuple[int, int]]:
    return [(span.start, span.end) for span in spans]


@pytest.mark.parametrize(
    "start,end",
    [(-1, 1), (0, 0), (2, 1), (False, 1), (0, True), (0.0, 2), (0, 2.0)],
)
def test_span_rejects_invalid_or_empty_ranges(start: Any, end: Any) -> None:
    with pytest.raises(ValueError):
        TextSpan(start, end)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"document_id": ""},
        {"block_id": ""},
        {"document_id": None},
        {"block_id": 1},
        {"projection_version": 0},
        {"projection_version": True},
        {"start": 0},
        {"end": 1},
        {"start": -1, "end": 1},
        {"start": 1, "end": 1},
        {"start": 0, "end": True},
    ],
)
def test_source_ref_validates_snapshot_and_range(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {"document_id": "doc", "block_id": "block"}
    with pytest.raises(ValueError):
        SourceRef(**(values | kwargs))


def test_chunk_and_references_are_frozen_without_a_persistent_identity() -> None:
    ref = SourceRef("doc", "block")
    chunk = Chunk("doc", " derived \n", (ref,), context_refs=(ref,))
    assert chunk.text == " derived \n"
    assert (ref.start, ref.end) == (None, None)
    assert {field.name for field in fields(chunk)} == {
        "document_id",
        "text",
        "source_refs",
        "ordinal",
        "context_refs",
    }
    for instance, field_name, value in (
        (chunk, "text", "edited"),
        (ref, "start", 0),
        (TextSpan(0, 1), "end", 2),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field_name, value)
        assert not hasattr(instance, "__dict__")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"document_id": ""},
        {"text": None},
        {"ordinal": -1},
        {"ordinal": True},
        {"source_refs": ("not a ref",)},
        {"context_refs": None},
        {"source_refs": (SourceRef("other", "p"),)},
        {"context_refs": (SourceRef("doc", "p", 2),)},
    ],
)
def test_chunk_rejects_invalid_values_and_mixed_provenance(
    kwargs: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "document_id": "doc",
        "text": "text",
        "source_refs": (SourceRef("doc", "p"),),
    }
    with pytest.raises(ValueError):
        Chunk(**(values | kwargs))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chunk_size": 0},
        {"chunk_size": -1},
        {"chunk_size": True},
        {"chunk_size": 1.5},
        {"chunk_overlap": -1},
        {"chunk_overlap": True},
        {"chunk_overlap": 1.5},
        {"chunk_overlap": 5},
        {"chunk_overlap": 6},
        {"separators": ()},
        {"separators": " "},
        {"separators": (" ", 0)},
        {"separators": None},
    ],
)
def test_recursive_rejects_invalid_configuration(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {"chunk_size": 5}
    with pytest.raises(ValueError):
        RecursiveTextChunker(**(values | kwargs))


def test_configs_freeze_caller_sequences_and_explain_budget_and_omissions() -> None:
    separators = ["\n", ""]
    chunker = RecursiveTextChunker(5, 1, separators)
    separators.append("changed")
    assert dict(chunker.config) == {
        "type": "recursive_text",
        "chunk_size": 5,
        "chunk_overlap": 1,
        "separators": ("\n", ""),
        "separator_position": "end",
        "length_unit": "python_characters",
    }
    composer = DocumentChunker(chunker)
    assert dict(composer.config) == {
        "type": "document",
        "text_chunker": chunker.config,
        "atomic_overflow": "reject",
        "atomic_join_separators": "omit",
        "synthetic_only": "omit",
    }
    for config in (chunker.config, composer.config):
        with pytest.raises(TypeError):
            config["changed"] = True  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        chunker.chunk_size = 10  # type: ignore[misc]


@pytest.mark.parametrize(
    "text,size,overlap,separators,expected",
    [
        ("", 5, 0, ("",), []),
        (" \n\t  ", 3, 0, ("",), [(0, 3), (3, 5)]),
        (" \n\t  ", 1000, 0, ("\n\n", "\n", " ", ""), [(0, 5)]),
        ("same same same", 5, 0, (" ", ""), [(0, 5), (5, 10), (10, 14)]),
        ("甲🙂é乙🙂é", 3, 1, ("",), [(0, 3), (2, 5), (4, 7), (6, 8)]),
        ("a\n\nb\n\n", 3, 0, ("\n\n", "\n", ""), [(0, 3), (3, 6)]),
        ("aa  bb  ", 4, 0, (" ", ""), [(0, 4), (4, 8)]),
        ("A::B::C", 4, 0, ("::", ""), [(0, 3), (3, 7)]),
        ("abcdefghi", 4, 1, ("absent",), [(0, 4), (3, 7), (6, 9)]),
        ("ABCDEFGHIJKL", 5, 1, ("",), [(0, 5), (4, 9), (8, 12)]),
        ("aa bb cc dd", 6, 3, (" ", ""), [(0, 6), (3, 9), (6, 11)]),
        ("aa bb cc dd", 6, 2, (" ", ""), [(0, 6), (6, 11)]),
        ("ab cdEFGHIJ xy", 5, 1, (" ", ""), [(0, 3), (3, 8), (7, 12), (12, 14)]),
    ],
)
def test_recursive_expected_offsets_preserve_whitespace_and_overlap(
    text: str,
    size: int,
    overlap: int,
    separators: tuple[str, ...],
    expected: list[tuple[int, int]],
) -> None:
    spans = RecursiveTextChunker(size, overlap, separators).split(text)
    assert ranges(spans) == expected
    assert all(0 <= span.start < span.end <= len(text) for span in spans)
    assert all(span.end - span.start <= size for span in spans)
    if not text:
        assert spans == []
        return
    assert spans[0].start == 0
    assert spans[-1].end == len(text)
    assert all(
        previous.start < current.start <= previous.end < current.end
        and max(previous.end - current.start, 0) <= overlap
        for previous, current in zip(spans, spans[1:], strict=False)
    )
    recovered = text[: spans[0].end]
    for previous, current in zip(spans, spans[1:], strict=False):
        recovered += text[previous.end : current.end]
    assert recovered == text


def test_recursive_rejects_nontext_input() -> None:
    with pytest.raises(TypeError, match="text must be a string"):
        RecursiveTextChunker().split(None)  # type: ignore[arg-type]


def test_document_cross_block_slice_maps_local_ranges_without_a_separator_owner() -> (
    None
):
    doc = document(paragraph("a", "ab"), paragraph("b", "cd"))
    chunks = DocumentChunker(RecursiveTextChunker(4, 1, ("",))).chunk(doc)
    assert [(chunk.text, chunk.ordinal) for chunk in chunks] == [
        ("ab\nc", 0),
        ("cd", 1),
    ]
    assert [chunk.source_refs for chunk in chunks] == [
        (
            SourceRef(doc.document_id, "a", 1, 0, 2),
            SourceRef(doc.document_id, "b", 1, 0, 1),
        ),
        (SourceRef(doc.document_id, "b", 1, 0, 2),),
    ]
    assert all(chunk.context_refs == () for chunk in chunks)
    assert all(chunk.document_id == doc.document_id for chunk in chunks)


def test_source_whitespace_remains_while_synthetic_only_slices_are_omitted() -> None:
    doc = document(paragraph("a", " "), paragraph("empty", ""), paragraph("b", "\t"))
    chunks = DocumentChunker(RecursiveTextChunker(1)).chunk(doc)
    assert [chunk.text for chunk in chunks] == [" ", "\t"]
    assert [chunk.source_refs for chunk in chunks] == [
        (SourceRef(doc.document_id, "a", 1, 0, 1),),
        (SourceRef(doc.document_id, "b", 1, 0, 1),),
    ]
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert DocumentChunker(RecursiveTextChunker()).chunk(document()) == ()
    assert (
        DocumentChunker(RecursiveTextChunker()).chunk(document(paragraph("p", "")))
        == ()
    )


def test_document_uses_rich_projection_including_code_and_formula_ranges() -> None:
    doc = document(
        Block(
            "p",
            "paragraph",
            rich_text=RichText(
                (
                    TextRun(" A  "),
                    InlineCode("x\t "),
                    InlineFormula(FormulaContent("x^2", "latex")),
                    TextRun("尾🙂\n"),
                )
            ),
        ),
        Block("code", "code", code="  a\n\n b "),
    )
    original_blocks = doc.blocks
    chunks = DocumentChunker(RecursiveTextChunker()).chunk(doc)
    assert len(chunks) == 1
    assert chunks[0].text == " A  x\t x^2尾🙂\n\n  a\n\n b "
    assert chunks[0].text == project_document(doc).text
    assert chunks[0].source_refs == (
        SourceRef(doc.document_id, "p", 1, 0, 13),
        SourceRef(doc.document_id, "code", 1, 0, 8),
    )
    assert doc.blocks is original_blocks


def test_whole_table_keeps_synthetic_cells_and_emits_children_once() -> None:
    doc = document(
        paragraph("before", "Before"),
        Block(
            "table",
            "table",
            table=TableContent(
                cells=(
                    TableCell(1, 2, ("formula",)),
                    TableCell(0, 0, ("left", "code"), row_span=2, column_span=2),
                ),
                rows=2,
                columns=3,
            ),
        ),
        paragraph("left", " 00123 ", "table"),
        Block("code", "code", code=" x\n y ", parent_block_id="table"),
        Block(
            "formula",
            "formula",
            formula=FormulaContent("12%", "latex"),
            parent_block_id="table",
        ),
        paragraph("after", "End"),
    )
    chunks = DocumentChunker(RecursiveTextChunker()).chunk(doc)
    assert [chunk.text for chunk in chunks] == [
        "Before",
        " 00123 \n x\n y \t\t\n\t\t12%",
        "End",
    ]
    assert [chunk.source_refs for chunk in chunks] == [
        (SourceRef(doc.document_id, "before", 1, 0, 6),),
        (SourceRef(doc.document_id, "table"),),
        (SourceRef(doc.document_id, "after", 1, 0, 3),),
    ]
    assert [chunk.ordinal for chunk in chunks] == [0, 1, 2]


def test_nested_tables_and_empty_grids_keep_authoritative_projection() -> None:
    doc = document(
        Block(
            "outer", "table", table=TableContent(cells=(TableCell(0, 0, ("inner",)),))
        ),
        Block(
            "inner",
            "table",
            table=TableContent(cells=(TableCell(0, 0, ("p",)),)),
            parent_block_id="outer",
        ),
        paragraph("p", "inside", "inner"),
        Block("gaps", "table", table=TableContent(cells=(), rows=2, columns=3)),
        paragraph("tail", "tail"),
    )
    chunks = DocumentChunker(RecursiveTextChunker()).chunk(doc)
    assert [chunk.text for chunk in chunks] == ["inside", "\t\t\n\t\t", "tail"]
    assert [chunk.source_refs for chunk in chunks] == [
        (SourceRef(doc.document_id, "outer"),),
        (SourceRef(doc.document_id, "gaps"),),
        (SourceRef(doc.document_id, "tail", 1, 0, 4),),
    ]


def test_images_and_asset_formulas_remain_whole_even_when_their_text_is_empty() -> None:
    pixels = media_asset()
    doc = document(
        Block("image", "image", asset_id="pixels"),
        Block("alt", "image", asset_id="pixels", alt_text=" A \n"),
        Block("formula", "formula", formula=FormulaContent(asset_id="pixels")),
        Block("both", "formula", formula=FormulaContent("x^2", "latex", "pixels")),
        Block(
            "inline",
            "paragraph",
            rich_text=RichText(
                (
                    TextRun("a"),
                    InlineFormula(FormulaContent(asset_id="pixels")),
                    TextRun(" b"),
                )
            ),
        ),
        Block(
            "only_inline",
            "paragraph",
            rich_text=RichText((InlineFormula(FormulaContent(asset_id="pixels")),)),
        ),
        assets=(pixels,),
    )
    chunks = DocumentChunker(RecursiveTextChunker(4)).chunk(doc)
    assert [chunk.text for chunk in chunks] == ["", " A \n", "", "x^2", "a b", ""]
    assert [chunk.source_refs for chunk in chunks] == [
        (SourceRef(doc.document_id, block_id),)
        for block_id in ("image", "alt", "formula", "both", "inline", "only_inline")
    ]


def test_nontextual_tables_and_their_resources_are_not_discarded() -> None:
    doc = document(
        paragraph("before", "before"),
        Block("html", "table", table=TableContent("html", html="<table></table>")),
        Block("image", "table", table=TableContent("image", asset_id="pixels")),
        paragraph("after", "after"),
        assets=(media_asset(),),
    )
    chunks = DocumentChunker(RecursiveTextChunker()).chunk(doc)
    assert [chunk.text for chunk in chunks] == ["before", "", "", "after"]
    assert chunks[1].source_refs == (SourceRef(doc.document_id, "html"),)
    assert chunks[2].source_refs == (SourceRef(doc.document_id, "image"),)


@pytest.mark.parametrize("kind", ["table", "image", "formula", "inline"])
def test_hard_budget_rejects_oversized_atomic_elements(kind: str) -> None:
    if kind == "table":
        blocks = (
            Block(
                "atomic",
                "table",
                table=TableContent(cells=(TableCell(0, 0, ("cell",)),)),
            ),
            paragraph("cell", "12345", "atomic"),
        )
    elif kind == "image":
        blocks = (Block("atomic", "image", asset_id="pixels", alt_text="12345"),)
    elif kind == "formula":
        blocks = (Block("atomic", "formula", formula=FormulaContent("12345", "latex")),)
    else:
        blocks = (
            Block(
                "atomic",
                "paragraph",
                rich_text=RichText(
                    (TextRun("12345"), InlineFormula(FormulaContent(asset_id="pixels")))
                ),
            ),
        )
    with pytest.raises(
        ValueError, match=r"atomic .* block 'atomic' exceeds chunk_size=4: 5 characters"
    ):
        DocumentChunker(RecursiveTextChunker(4)).chunk(
            document(*blocks, assets=(media_asset(),))
        )
    assert (
        DocumentChunker(RecursiveTextChunker(5))
        .chunk(document(*blocks, assets=(media_asset(),)))[0]
        .text
        == "12345"
    )


def test_atomic_table_budget_includes_synthetic_cell_and_row_separators() -> None:
    doc = document(
        Block("grid", "table", table=TableContent(cells=(), rows=2, columns=3))
    )
    with pytest.raises(
        ValueError, match=r"block 'grid' exceeds chunk_size=4: 5 characters"
    ):
        DocumentChunker(RecursiveTextChunker(4)).chunk(doc)
    assert DocumentChunker(RecursiveTextChunker(5)).chunk(doc)[0].text == "\t\t\n\t\t"


def test_document_rejects_invalid_inputs() -> None:
    with pytest.raises(TypeError, match="RecursiveTextChunker"):
        DocumentChunker(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ParsedDocument"):
        DocumentChunker(RecursiveTextChunker()).chunk("text")  # type: ignore[arg-type]


def test_chunker_imports_and_execution_need_no_model_or_storage_sdk() -> None:
    script = """
import sys
from importlib.abc import MetaPathFinder

class NoSDK(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'tiktoken', 'transformers', 'openai', 'sqlalchemy', 'pymilvus'}:
            raise RuntimeError(f'unexpected SDK import: {fullname}')
        return None

sys.meta_path.insert(0, NoSDK())
from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker, SourceRef
assert RecursiveTextChunker(3).split('  ')
assert SourceRef('doc', 'image').start is None
"""
    # 仅执行当前解释器与本测试内的固定脚本。
    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
