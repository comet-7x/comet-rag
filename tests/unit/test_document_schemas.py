from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from comet_rag.content import MediaRef
from comet_rag.documents.codec import document_from_dict, document_to_dict, dumps, loads
from comet_rag.documents.projection import project_document
from comet_rag.documents.schemas import (
    Asset,
    Block,
    BlockRelation,
    FormulaContent,
    InlineCode,
    InlineFormula,
    Location,
    PageInfo,
    ParsedDocument,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParsingInfo,
    RichText,
    SourceInfo,
    TableCell,
    TableContent,
    TextRun,
    TextStyle,
)

SOURCE = SourceInfo("source-1", "a" * 64, "file:///data/original.docx", "v1")
PARSING = ParsingInfo("native-docx", "1.0", {"preserve_whitespace": True})


def paragraph(block_id: str, text: str, parent: str | None = None) -> Block:
    return Block(
        block_id,
        "paragraph",
        rich_text=RichText((TextRun(text),)),
        parent_block_id=parent,
    )


def document(*blocks: Block, **kwargs: Any) -> ParsedDocument:
    return ParsedDocument(SOURCE, PARSING, blocks, **kwargs)


def asset(
    asset_id: str = "formula-pixels", path: str = "/retained/formula.png"
) -> Asset:
    return Asset(
        asset_id,
        "b" * 64,
        MediaRef(path=Path(path), media_type="image/png"),
        width=8,
        height=9,
    )


def test_rich_projection_preserves_unicode_spaces_code_formula_and_block_offsets() -> (
    None
):
    text = RichText(
        (
            TextRun(
                " A  ",
                TextStyle(bold=True, color="#aaBBcc"),
                "https://example.org/read?q=1",
            ),
            InlineCode("x\t "),
            InlineFormula(FormulaContent(" x^2 ", "latex")),
            TextRun("尾🙂\n"),
        )
    )
    doc = document(
        Block("p", "heading", rich_text=text, level=2),
        Block("c", "code", code="  a\n\n b ", language="py"),
    )
    projection = project_document(doc)
    assert projection.text == " A  x\t  x^2 尾🙂\n\n  a\n\n b "
    assert [
        (span.start, span.end, span.block_id, span.block_start, span.block_end)
        for span in projection.spans
    ] == [
        (0, 4, "p", 0, 4),
        (4, 7, "p", 4, 7),
        (7, 12, "p", 7, 12),
        (12, 15, "p", 12, 15),
        (15, 16, None, None, None),
        (16, 24, "c", 0, 8),
    ]
    assert projection.document_id == doc.document_id
    assert projection.projection_version == 1
    assert projection.limitations == ()
    assert text.inlines[0] == TextRun(
        " A  ", TextStyle(bold=True, color="#AABBCC"), "https://example.org/read?q=1"
    )


def rich_table() -> ParsedDocument:
    table = Block(
        "table",
        "table",
        table=TableContent(
            cells=(
                TableCell(1, 2, ("formula",)),
                TableCell(0, 2, ()),
                TableCell(
                    0, 0, ("left", "code"), row_span=2, column_span=2, header=None
                ),
            ),
            rows=2,
            columns=3,
        ),
    )
    return document(
        paragraph("before", " Intro "),
        table,
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


def test_merged_table_uses_cell_coordinates_and_global_cell_subsequence_once() -> None:
    projection = project_document(rich_table())
    assert projection.text == " Intro \n 00123 \n x\n y \t\t\n\t\t12%\nEnd"
    spans = projection.spans
    assert [span.block_id for span in spans if span.block_id is not None] == [
        "before",
        "left",
        "code",
        "formula",
        "after",
    ]
    assert [
        projection.text[span.start : span.end]
        for span in spans
        if span.block_id is None
    ] == ["\n", "\n", "\t", "\t", "\n", "\t", "\t", "\n"]
    assert projection.limitations == ()
    assert loads(dumps(rich_table())) == rich_table()


def test_nested_tables_render_once_with_each_childs_local_range() -> None:
    outer = Block(
        "outer", "table", table=TableContent(cells=(TableCell(0, 0, ("inner",)),))
    )
    inner = Block(
        "inner",
        "table",
        table=TableContent(cells=(TableCell(0, 0, ("p",)),)),
        parent_block_id="outer",
    )
    projection = project_document(
        document(outer, inner, paragraph("p", "inside", "inner"))
    )
    assert projection.text == "inside"
    assert [
        (span.block_id, span.block_start, span.block_end) for span in projection.spans
    ] == [("p", 0, 6)]


def test_image_formulas_and_unknown_tables_remain_explicit_projection_limitations() -> (
    None
):
    image_formula = FormulaContent(asset_id="formula-pixels")
    doc = document(
        Block(
            "p",
            "paragraph",
            rich_text=RichText(
                (TextRun("a"), InlineFormula(image_formula), TextRun(" b"))
            ),
        ),
        Block("f", "formula", formula=image_formula),
        Block(
            "html",
            "table",
            table=TableContent("html", html="<table><tr><td>x</td></tr></table>"),
        ),
        Block("image", "table", table=TableContent("image", asset_id="formula-pixels")),
        Block(
            "gaps",
            "table",
            table=TableContent(cells=(TableCell(0, 0),), rows=1, columns=2),
        ),
        assets=(asset(),),
    )
    projection = project_document(doc)
    assert projection.text == "a b\n\n\n\n\t"
    assert [
        (issue.block_id, issue.code, issue.offset, issue.asset_id)
        for issue in projection.limitations
    ] == [
        ("p", "image_only_formula", 1, "formula-pixels"),
        ("f", "image_only_formula", 0, "formula-pixels"),
        ("html", "table_html_not_textualized", 0, None),
        ("image", "table_image_not_textualized", 0, "formula-pixels"),
        ("gaps", "table_unknown_cells", 0, None),
    ]
    assert loads(dumps(doc)) == doc


def test_image_occurrences_share_asset_without_generating_text() -> None:
    doc = document(
        Block("i1", "image", asset_id="formula-pixels", alt_text="Original caption"),
        Block("i2", "image", asset_id="formula-pixels"),
        assets=(asset(),),
    )
    projection = project_document(doc)
    assert projection.text == "Original caption\n"
    assert [(item.block_id, item.code) for item in projection.limitations] == [
        ("i2", "image_without_text")
    ]


@pytest.mark.parametrize(
    "blocks, message",
    [
        ((paragraph("a", "x"), paragraph("a", "y")), "duplicate"),
        ((paragraph("a", "x", "absent"),), "missing parent"),
        ((paragraph("a", "x", "a"),), "cycle"),
        ((paragraph("a", "x", "b"), paragraph("b", "x", "a")), "cycle"),
        (
            (
                replace(
                    paragraph("a", "x"),
                    relations=(BlockRelation("caption_of", "missing"),),
                ),
            ),
            "missing related",
        ),
        ((Block("i", "image", asset_id="missing"),), "missing block asset"),
        (
            (Block("f", "formula", formula=FormulaContent(asset_id="missing")),),
            "missing block asset",
        ),
        (
            (
                Block(
                    "f",
                    "paragraph",
                    rich_text=RichText(
                        (InlineFormula(FormulaContent(asset_id="missing")),)
                    ),
                ),
            ),
            "missing block asset",
        ),
        (
            (Block("t", "table", table=TableContent("image", asset_id="missing")),),
            "missing block asset",
        ),
    ],
)
def test_document_rejects_missing_references_duplicate_ids_and_cycles(
    blocks: tuple[Block, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        document(*blocks)


@pytest.mark.parametrize(
    "members, children, message",
    [
        (("absent",), (), "must exist"),
        (("a",), (paragraph("a", "x"),), "same table"),
        (
            ("a",),
            (
                paragraph("a", "x", "other"),
                Block("other", "table", table=TableContent(cells=())),
            ),
            "same table",
        ),
        (
            ("b", "a"),
            (paragraph("a", "x", "t"), paragraph("b", "y", "t")),
            "cell order",
        ),
        (("a", "a"), (paragraph("a", "x", "t"),), "multiple cells"),
        ((), (paragraph("a", "x", "t"),), "must belong to a cell"),
    ],
)
def test_cell_references_have_one_owner_and_agree_with_global_order(
    members: tuple[str, ...], children: tuple[Block, ...], message: str
) -> None:
    table = Block("t", "table", table=TableContent(cells=(TableCell(0, 0, members),)))
    with pytest.raises(ValueError, match=message):
        document(table, *children)


def test_same_child_cannot_belong_to_two_cells() -> None:
    table = Block(
        "t",
        "table",
        table=TableContent(cells=(TableCell(0, 0, ("a",)), TableCell(0, 1, ("a",)))),
    )
    with pytest.raises(ValueError, match="multiple cells"):
        document(table, paragraph("a", "x", "t"))


@pytest.mark.parametrize(
    "cells",
    [
        (TableCell(0, 0, row_span=2, column_span=2), TableCell(1, 1)),
        (TableCell(0, 0), TableCell(0, 0)),
    ],
)
def test_merged_cells_cannot_overlap(cells: tuple[TableCell, ...]) -> None:
    with pytest.raises(ValueError, match="overlapping"):
        TableContent(cells=cells)


def test_table_dimensions_spans_and_exclusive_fallback_are_validated() -> None:
    cases: list[dict[str, Any]] = [
        {},
        {"cells": (), "html": "<table/>"},
        {"representation": "html", "cells": ()},
        {"representation": "image", "html": "<table/>"},
    ]
    for kwargs in cases:
        with pytest.raises(ValueError, match="primary representation"):
            TableContent(**kwargs)
    with pytest.raises(ValueError, match="exceeds"):
        TableContent(cells=(TableCell(1, 1, row_span=2),), rows=2, columns=2)
    with pytest.raises(ValueError, match="span"):
        TableCell(0, 0, column_span=0)
    assert (
        document(Block("t", "table", table=TableContent(cells=()))).blocks[0].table
        is not None
    )


def test_locations_are_native_coordinates_and_reference_real_pages() -> None:
    page = PageInfo("p1", width=612, height=792, unit="pt", asset_id="formula-pixels")
    location = Location("p1", "paragraph:4", 3, 8, (10, 20, 50, 60), "pt", "top-left")
    doc = document(
        replace(paragraph("b", "text"), locations=(location,)),
        pages=(page,),
        assets=(asset(),),
    )
    assert loads(dumps(doc)) == doc
    assert doc.blocks[0].locations[0].coordinate_space == "source"
    with pytest.raises(ValueError, match="missing location page"):
        document(replace(paragraph("b", "text"), locations=(location,)))
    with pytest.raises(ValueError, match="units differ"):
        document(
            replace(paragraph("b", "text"), locations=(replace(location, unit="px"),)),
            pages=(page,),
            assets=(asset(),),
        )
    with pytest.raises(ValueError, match="missing or self"):
        document(pages=(page,))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bbox": (0, 0, 1, 1)},
        {"start": 1},
        {"start": 4, "end": 2},
        {"unit": "pt"},
        {
            "page_id": "p",
            "bbox": (0, 0, 2, 1),
            "unit": "normalized",
            "origin": "top-left",
        },
        {
            "page_id": "p",
            "bbox": (0, 0, float("nan"), 1),
            "unit": "px",
            "origin": "top-left",
        },
        {"coordinate_space": "projection"},
        {"coordinate_version": 2},
    ],
)
def test_invalid_locations_fail(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Location(**kwargs)


def test_snapshot_identity_independent_known_digest_and_content_changes() -> None:
    doc = ParsedDocument(SourceInfo("s", "a" * 64), ParsingInfo("engine"))
    expected_payload = {
        "type": "document",
        "source": {
            "type": "source",
            "source_id": "s",
            "content_hash": "a" * 64,
            "source_version": None,
        },
        "parsing": {
            "type": "parsing",
            "engine": "engine",
            "version": None,
            "config": {},
        },
        "blocks": [],
        "assets": [],
        "pages": [],
        "schema_version": 1,
        "projection_version": 1,
    }
    encoded = json.dumps(
        {"kind": "document", "version": 1, "value": expected_payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert doc.document_id == "document_" + sha256(encoded).hexdigest()
    original = document(paragraph("p", " a "))
    assert (
        replace(original, blocks=(paragraph("p", "a"),)).document_id
        != original.document_id
    )
    assert (
        replace(original, parsing=replace(PARSING, version="2")).document_id
        != original.document_id
    )
    assert (
        replace(
            original, parsing=replace(PARSING, config={"preserve_whitespace": False})
        ).document_id
        != original.document_id
    )
    assert (
        replace(original, source=replace(SOURCE, content_hash="c" * 64)).document_id
        != original.document_id
    )
    assert (
        replace(original, source=replace(SOURCE, source_id="other")).document_id
        != original.document_id
    )


def test_identity_excludes_paths_and_transient_metadata_but_covers_assets() -> None:
    doc = document(paragraph("p", "x"), assets=(asset(),), metadata={"elapsed_ms": 10})
    relocated = replace(
        doc,
        source=replace(SOURCE, locator="file:///another/source.docx"),
        assets=(
            replace(
                asset(),
                media=MediaRef(path="/new/image.png", media_type="image/png"),
                name="new.png",
            ),
        ),
        blocks=(replace(doc.blocks[0], metadata={"runtime": 5}),),
        metadata={"elapsed_ms": 90},
    )
    assert doc.document_id == relocated.document_id
    assert (
        replace(doc, assets=(replace(asset(), content_hash="c" * 64),)).document_id
        != doc.document_id
    )
    assert (
        replace(
            doc, blocks=(replace(doc.blocks[0], layout={"alignment": "center"}),)
        ).document_id
        != doc.document_id
    )
    assert document_to_dict(doc) != document_to_dict(relocated)


def test_json_inputs_are_defensively_frozen_and_reject_non_json_values() -> None:
    config: dict[str, Any] = {"nested": {"numbers": [1, 2]}}
    metadata: dict[str, Any] = {"tags": ["original"]}
    parsing = ParsingInfo("e", config=config)
    doc = ParsedDocument(
        SOURCE,
        parsing,
        (replace(paragraph("p", "x"), metadata=metadata),),
        metadata=metadata,
    )
    snapshot_id = doc.document_id
    config["nested"]["numbers"].append(3)
    metadata["tags"].append("new")
    assert doc.parsing.config["nested"] == {"numbers": (1, 2)}
    assert doc.metadata["tags"] == ("original",)
    assert doc.blocks[0].metadata["tags"] == ("original",)
    assert doc.document_id == snapshot_id
    with pytest.raises(FrozenInstanceError):
        doc.document_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        doc.metadata["new"] = True  # type: ignore[index]
    for invalid in (
        float("nan"),
        float("inf"),
        Path("/retained/runtime"),
        {1: "nonstring key"},
    ):
        with pytest.raises(ValueError):
            ParsingInfo("e", config={"bad": invalid})


def test_result_roundtrip_keeps_diagnostics_coverage_and_blank_document() -> None:
    issue = ParseIssue(
        "page_failed", "page 2 could not be decoded", "error", (Location("p2"),)
    )
    report = ParseReport(
        "partial", (issue,), {"requested_pages": [1, 2], "processed_pages": [1]}
    )
    result = ParseResult(document(pages=(PageInfo("p2", number=2),)), report)
    restored = loads(dumps(result))
    assert restored == result
    assert isinstance(restored, ParseResult)
    assert restored.document.blocks == ()
    assert restored.report.coverage == {
        "requested_pages": (1, 2),
        "processed_pages": (1,),
    }
    assert loads(dumps(ParseResult(document()))) == ParseResult(document())
    with pytest.raises(ValueError, match="status"):
        ParseReport("partial")
    with pytest.raises(ValueError, match="status"):
        ParseReport("complete", (issue,))
    with pytest.raises(ValueError, match="missing location page"):
        ParseResult(document(), report)


def test_binary_and_resource_provenance_roundtrip_and_hash_guards() -> None:
    data = b"image-data"
    image = Asset(
        "pixels",
        sha256(data).hexdigest(),
        MediaRef(data=data, media_type="image/png"),
        size_bytes=10,
        provenance="page_render",
    )
    cropped = replace(
        asset("crop"), source_asset_ids=("pixels",), provenance="region_crop"
    )
    doc = document(Block("image", "image", asset_id="crop"), assets=(image, cropped))
    assert loads(dumps(doc)) == doc
    with pytest.raises(ValueError, match="do not match"):
        replace(image, content_hash="0" * 64)
    with pytest.raises(ValueError, match="size"):
        replace(image, size_bytes=9)
    with pytest.raises(ValueError, match="missing or self"):
        document(assets=(cropped,))
    with pytest.raises(ValueError, match="duplicate"):
        document(assets=(image, image))


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 2},
        {"projection_version": 2},
        {"schema_version": True},
        {"document_id": "document_wrong"},
        {"future": 1},
    ],
)
def test_codec_rejects_unknown_versions_fields_and_incorrect_identity(
    mutation: dict[str, object],
) -> None:
    encoded = document_to_dict(document(paragraph("p", "x")))
    encoded.update(mutation)
    with pytest.raises(ValueError):
        document_from_dict(encoded)


def test_codec_nested_unknown_fields_and_tags_never_disappear() -> None:
    encoded: Any = document_to_dict(document(paragraph("p", "x")))
    encoded["blocks"][0]["rich_text"]["inlines"][0]["future"] = "silently dropped?"
    with pytest.raises(ValueError, match="unknown text_run fields"):
        document_from_dict(encoded)
    del encoded["blocks"][0]["rich_text"]["inlines"][0]["future"]
    encoded["blocks"][0]["rich_text"]["inlines"][0]["type"] = "new_inline"
    with pytest.raises(ValueError, match="inline tag"):
        document_from_dict(encoded)
    encoded = document_to_dict(document())
    del encoded["schema_version"]
    with pytest.raises(ValueError, match="explicit versions"):
        document_from_dict(encoded)
    with pytest.raises(ValueError, match="duplicate JSON"):
        loads('{"type":"document","type":"result"}')
    with pytest.raises(ValueError, match="invalid JSON number"):
        loads('{"type":"document","schema_version":NaN}')


def test_codec_preserves_opaque_user_json_keys_instead_of_treating_them_as_tags() -> (
    None
):
    metadata = {
        "type": "unknown",
        "blocks": [{"not": "a block"}],
        "document_id": "opaque",
        "style": {"future": 1},
    }
    doc = replace(
        document(), metadata=metadata, parsing=replace(PARSING, config=metadata)
    )
    assert loads(dumps(doc)) == doc


@pytest.mark.parametrize(
    "locator",
    [
        "https://user@example.org/file",
        "https://example.org/file?ToKeN=private",
        "https://example.org/file?X-Amz-Signature=private",
        "https://example.org/file?api_key=private",
        "https://example.org/file?X-Goog-Credential=private",
        "https://example.org/file?Auth=private",
        "https://example.org/file?authorization=private",
        "https://example.org/file?credential=private",
        "https://example.org/file?credentials=private",
        "https://example.org/file?password=private",
        "https://example.org/file?secret=private",
        "https://example.org/file?AWSAccessKeyId=private",
        "https://example.org/file?token=",
        "https://example.org/file?%61uth=private",
        "https://example.org/file?client_secret=private",
        "https://example.org/file?refresh_token=private",
        "https://example.org/file?api-key=private",
        "https://example.org/file?q-signature=private",
        "https://example.org/file?%43lient%5FSecret=private",
    ],
)
def test_persistent_locators_reject_known_credentials_without_stripping(
    locator: str,
) -> None:
    with pytest.raises(ValueError, match="credentials"):
        SourceInfo("s", "a" * 64, locator)
    with pytest.raises(ValueError, match="credentials"):
        TextRun("link", link=locator)
    if "user@" not in locator:
        doc = document(assets=(replace(asset(), media=MediaRef(url=locator)),))
        with pytest.raises(ValueError, match="credentials"):
            dumps(doc)
    stable = "https://example.org/file?key=ordinary&revision=3"
    assert SourceInfo("s", "a" * 64, stable).locator == stable


@pytest.mark.parametrize(
    "encoded",
    [
        '{"type":[]}',
        '{"type":"result","document":null}',
        '{"type":"result","document":{"type":[]}}',
    ],
)
def test_codec_malformed_tags_raise_value_error(encoded: str) -> None:
    with pytest.raises(ValueError):
        loads(encoded)


def test_codec_rejects_malformed_media_and_preserves_live_url_identity() -> None:
    doc = document(assets=(asset(),))
    encoded: Any = document_to_dict(doc)
    encoded["assets"][0]["media"]["url"] = 42
    with pytest.raises(ValueError, match="media values"):
        document_from_dict(encoded)
    encoded = document_to_dict(doc)
    encoded["assets"][0]["media"].update(path=None, data="!invalid-base64!")
    with pytest.raises(ValueError, match="base64"):
        document_from_dict(encoded)
    encoded = document_to_dict(doc)
    encoded["parsing"]["config"] = [1, 2]
    with pytest.raises(ValueError, match="JSON object"):
        document_from_dict(encoded)
