from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker, SourceRef
from comet_rag.content import ImagePart, MediaRef, TextPart, text_view
from comet_rag.documents.schemas import (
    Asset,
    Block,
    FormulaContent,
    InlineCode,
    InlineFormula,
    Location,
    ParsedDocument,
    ParsingInfo,
    RichText,
    SourceInfo,
    TableCell,
    TableContent,
    TextRun,
    TextStyle,
)
from comet_rag.embedder import EmbeddingSpace
from comet_rag.identity import canonical_json, stable_id
from comet_rag.indexing.builders.flat import FlatIndexBuilder
from comet_rag.indexing.codec import from_dict, to_dict
from comet_rag.indexing.types import IndexUnit, Representation, content_fingerprint


def document(*blocks: Block, assets: tuple[Asset, ...] = ()) -> ParsedDocument:
    return ParsedDocument(
        SourceInfo("source", sha256(b"original").hexdigest()),
        ParsingInfo("test"),
        blocks,
        assets=assets,
        metadata={"category": "science"},
    )


def asset(name: str, *, sources: tuple[str, ...] = ()) -> Asset:
    data = name.encode()
    return Asset(
        name,
        sha256(data).hexdigest(),
        MediaRef(data=data, media_type="image/png"),
        source_asset_ids=sources,
    )


def builder(
    strategy: Literal["text", "media", "joint", "separate"] = "text", size: int = 100
) -> FlatIndexBuilder:
    return FlatIndexBuilder(
        DocumentChunker(RecursiveTextChunker(size)), representation=strategy
    )


def test_materialized_table_keeps_child_blocks_metadata_and_actual_inputs() -> None:
    doc = document(
        Block(
            "table", "table", table=TableContent(cells=(TableCell(0, 0, ("cell",)),))
        ),
        Block(
            "cell",
            "paragraph",
            parent_block_id="table",
            rich_text=RichText((TextRun(" 00123 "),)),
        ),
    )
    built = builder().build(doc, index_scope="scope", index_profile_id="profile")
    assert len(built.units) == len(built.representations) == 1
    unit, rep = built.units[0], built.representations[0]
    assert unit.blocks == doc.blocks and unit.metadata == {"category": "science"}
    assert text_view(unit.content) == " 00123 "
    assert rep.content == (TextPart(" 00123 "),) and rep.source_refs == unit.source_refs
    for value in (unit, rep):
        assert from_dict(to_dict(value)) == value


def test_text_units_retain_selected_ranges_without_duplicating_the_source_block() -> (
    None
):
    text = "0123456789" * 2_000
    doc = document(Block("b", "text", rich_text=RichText((TextRun(text),))))
    built = builder(size=100).build(doc, index_scope="s", index_profile_id="p")
    stored = [to_dict(unit) for unit in built.units]
    assert len(built.units) == 200
    assert [text_view(unit.content) for unit in built.units] == [
        text[i : i + 100] for i in range(0, len(text), 100)
    ]
    assert [unit.blocks[0].rich_text for unit in built.units] == [
        RichText((TextRun(text[i : i + 100]),)) for i in range(0, len(text), 100)
    ]
    assert [
        (unit.source_refs[0].start, unit.source_refs[0].end) for unit in built.units
    ] == [(i, i + 100) for i in range(0, len(text), 100)]
    assert sum(len(canonical_json(value)) for value in stored) < len(text) * 20
    assert doc.blocks[0].rich_text == RichText((TextRun(text),))


def test_partial_rich_text_and_code_keep_style_links_whitespace_and_source_coordinates() -> (
    None
):
    location = Location(element_id="original-paragraph", start=10, end=20)
    style = TextStyle(bold=True, color="#123456")
    doc = document(
        Block(
            "rich",
            "paragraph",
            rich_text=RichText(
                (
                    TextRun("ab", style, "https://example.org"),
                    InlineCode(" c\t"),
                    TextRun("def", TextStyle(italic=True)),
                )
            ),
            locations=(location,),
        ),
        Block(
            "code", "code", code="  f()\n  g()", language="python", filename="sample.py"
        ),
    )
    flat = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(4, separators=("",))))
    built = flat.build(doc, index_scope="s", index_profile_id="p")
    first, second = built.units[:2]
    assert first.content == (TextPart("ab c"),)
    assert first.blocks[0].rich_text == RichText(
        (TextRun("ab", style, "https://example.org"), InlineCode(" c"))
    )
    assert second.content == (TextPart("\tdef"),)
    assert second.blocks[0].rich_text == RichText(
        (InlineCode("\t"), TextRun("def", TextStyle(italic=True)))
    )
    assert second.source_refs == (SourceRef(doc.document_id, "rich", start=4, end=8),)
    assert first.blocks[0].locations == second.blocks[0].locations == (location,)
    code_units = [
        unit
        for unit in built.units
        if any(ref.block_id == "code" for ref in unit.source_refs)
    ]
    assert (
        "".join(
            block.code or ""
            for unit in code_units
            for block in unit.blocks
            if block.block_id == "code"
        )
        == "  f()\n  g()"
    )
    assert all(
        block.language == "python" and block.filename == "sample.py"
        for unit in code_units
        for block in unit.blocks
        if block.block_id == "code"
    )
    assert all(from_dict(to_dict(unit)) == unit for unit in built.units)


def test_selected_formula_source_remains_whole_when_chunk_crosses_its_expression() -> (
    None
):
    formula = InlineFormula(FormulaContent("x^2+y^2", "latex"))
    doc = document(
        Block(
            "b", "paragraph", rich_text=RichText((TextRun("a"), formula, TextRun("z")))
        )
    )
    flat = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(4, separators=("",))))
    built = flat.build(doc, index_scope="s", index_profile_id="p")
    assert [text_view(unit.content) for unit in built.units] == ["ax^2", "+y^2", "z"]
    assert [unit.blocks[0].rich_text for unit in built.units] == [
        RichText((TextRun("a"), formula)),
        RichText((formula,)),
        RichText((TextRun("z"),)),
    ]
    assert [
        (unit.source_refs[0].start, unit.source_refs[0].end) for unit in built.units
    ] == [(0, 4), (4, 8), (8, 9)]


@pytest.mark.parametrize("kind", ["image", "formula", "inline_formula", "table"])
def test_all_image_payloads_materialize_without_embedding_provenance_images(
    kind: str,
) -> None:
    original = asset("original")
    derived = asset("derived", sources=(original.asset_id,))
    if kind == "image":
        block = Block("b", "image", asset_id=derived.asset_id)
    elif kind == "formula":
        block = Block("b", "formula", formula=FormulaContent(asset_id=derived.asset_id))
    elif kind == "inline_formula":
        block = Block(
            "b",
            "paragraph",
            rich_text=RichText(
                (InlineFormula(FormulaContent(asset_id=derived.asset_id)),)
            ),
        )
    else:
        block = Block(
            "b",
            "table",
            table=TableContent(representation="image", asset_id=derived.asset_id),
        )
    doc = document(block, assets=(original, derived))
    built = builder("separate").build(
        doc, index_scope="scope", index_profile_id="profile"
    )
    assert built.units[0].content == (ImagePart(derived.media),)
    assert built.units[0].assets == (derived, original)
    assert built.representations[0].content == (ImagePart(derived.media),)
    assert from_dict(to_dict(built.units[0])) == built.units[0]
    with pytest.raises(ValueError, match="text view"):
        builder().build(doc, index_scope="scope", index_profile_id="profile")


@pytest.mark.parametrize("strategy", ["media", "joint", "separate"])
def test_repeated_image_occurrences_share_asset_without_dropping_inputs(
    strategy: Literal["media", "joint", "separate"],
) -> None:
    pixels = asset("logo")
    doc = document(
        Block(
            "table",
            "table",
            table=TableContent(
                cells=(TableCell(0, 0, ("left",)), TableCell(0, 1, ("right",)))
            ),
        ),
        Block("left", "image", asset_id="logo", parent_block_id="table"),
        Block("right", "image", asset_id="logo", parent_block_id="table"),
        assets=(pixels,),
    )
    built = builder(strategy).build(doc, index_scope="s", index_profile_id="p")
    (unit,) = built.units
    assert unit.assets == (pixels,)
    assert [part for part in unit.content if isinstance(part, ImagePart)] == [
        ImagePart(pixels.media),
        ImagePart(pixels.media),
    ]
    assert [
        part
        for rep in built.representations
        for part in rep.content
        if isinstance(part, ImagePart)
    ] == [ImagePart(pixels.media), ImagePart(pixels.media)]
    assert all(
        from_dict(to_dict(value)) == value for value in (unit, *built.representations)
    )


def test_input_fingerprints_use_actual_bytes_not_access_path_and_keep_order(
    tmp_path: Path,
) -> None:
    first = asset("image")
    second = replace(
        first, media=MediaRef(path=tmp_path / "same.png", media_type="image/png")
    )
    assert content_fingerprint(
        (ImagePart(first.media),), (first,)
    ) == content_fingerprint((ImagePart(second.media),), (second,))
    assert content_fingerprint(
        (TextPart(" a "), ImagePart(first.media)), (first,)
    ) != content_fingerprint((ImagePart(first.media), TextPart(" a ")), (first,))
    with pytest.raises(ValueError, match="verified"):
        content_fingerprint((ImagePart(second.media),))


def test_profile_and_unit_identity_bind_scope_strategy_chunking_and_space() -> None:
    doc = document(Block("b", "paragraph", rich_text=RichText((TextRun("abc"),))))
    flat = builder()
    space = EmbeddingSpace("model", 2, "local", version="1")
    profile = flat.profile_id(space)
    previous_profile = stable_id(
        "profile",
        {
            "builder": "flat_v2",
            "chunker": flat.chunker.config,
            "representations": flat.representation,
            "space": space.space_id,
        },
    )
    assert profile != previous_profile
    assert profile != builder(size=2).profile_id(space)
    assert profile != builder("joint").profile_id(space)
    assert profile != flat.profile_id(replace(space, version="2"))
    first = flat.build(doc, index_scope="a", index_profile_id=profile)
    other = flat.build(doc, index_scope="b", index_profile_id=profile)
    assert first.units[0].unit_id != other.units[0].unit_id
    assert doc.document_id == first.units[0].document_id == other.units[0].document_id


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"type": "unknown"},
        {"unit_id": "forged"},
        {"extra": "unexpected"},
        {"content": [{"type": "text_part", "text": "altered"}]},
        {
            "source_refs": [
                {
                    "document_id": "other",
                    "block_id": "b",
                    "projection_version": 1,
                    "start": 0,
                    "end": 1,
                }
            ]
        },
    ],
)
def test_codec_rejects_unsupported_version_fields_and_altered_identity(
    change: dict,
) -> None:
    doc = document(Block("b", "paragraph", rich_text=RichText((TextRun("abc"),))))
    unit = builder().build(doc, index_scope="s", index_profile_id="p").units[0]
    data = deepcopy(to_dict(unit))
    data.update(change)
    with pytest.raises(ValueError):
        from_dict(data)


def _text_unit() -> IndexUnit:
    return IndexUnit(
        "scope",
        "profile",
        "source",
        "document",
        "actual text",
        (SourceRef("document", "b"),),
    )


def _text_representation() -> Representation:
    unit = _text_unit()
    return Representation(
        unit.unit_id,
        unit.index_scope,
        unit.index_profile_id,
        unit.source_id,
        unit.document_id,
        unit.content,
        source_refs=unit.source_refs,
    )


@pytest.mark.parametrize(
    "field", ["index_scope", "index_profile_id", "source_id", "document_id"]
)
def test_representation_identity_binds_all_persisted_scope_and_snapshot_fields(
    field: str,
) -> None:
    original = _text_representation()
    data = to_dict(original)
    data[field] = "changed"
    if field == "document_id":
        data["source_refs"][0]["document_id"] = "changed"
    with pytest.raises(ValueError, match="identity"):
        from_dict(data)
    values: dict[str, Any] = {field: "changed"}
    if field == "document_id":
        values["source_refs"] = (SourceRef("changed", "b"),)
    assert replace(original, **values).representation_id != original.representation_id


@pytest.mark.parametrize(
    "field", ["content", "source_refs", "assets", "locations", "blocks", "context_refs"]
)
@pytest.mark.parametrize("invalid", ["", {}, None, ()])
def test_indexing_codec_requires_json_arrays_for_all_sequence_fields(
    field: str, invalid: Any
) -> None:
    value = _text_representation() if field == "context_refs" else _text_unit()
    data = to_dict(value)
    data[field] = invalid
    with pytest.raises(ValueError, match="array"):
        from_dict(data)


@pytest.mark.parametrize("ref", [{}, {"unexpected": 1}, None, "", []])
def test_indexing_codec_rejects_malformed_source_reference_objects(ref: Any) -> None:
    data = to_dict(_text_unit())
    data["source_refs"] = [ref]
    with pytest.raises(ValueError, match="source ref"):
        from_dict(data)


@pytest.mark.parametrize("value", [1, True, "", " "])
def test_unit_parent_requires_nonblank_string(value: Any) -> None:
    with pytest.raises(ValueError, match="parent ID"):
        replace(_text_unit(), parent_unit_id=value)


def test_indexing_inputs_require_arrays_and_consistent_projection_versions() -> None:
    unit, rep = _text_unit(), _text_representation()
    for field in ("assets", "locations", "blocks"):
        with pytest.raises(ValueError, match=field):
            replace(unit, **{field: ""})
    with pytest.raises(ValueError, match="assets"):
        replace(rep, assets=cast("Any", ""))
    refs = (
        SourceRef("document", "b"),
        SourceRef("document", "other", projection_version=2),
    )
    with pytest.raises(ValueError, match="projection_version"):
        replace(unit, source_refs=refs)
    with pytest.raises(ValueError, match="projection_version"):
        replace(rep, context_refs=refs[1:])


@pytest.mark.parametrize("kind", ["image", "formula", "inline_formula", "table"])
def test_unit_rejects_missing_assets_from_every_block_payload(kind: str) -> None:
    if kind == "image":
        block = Block("b", "image", asset_id="missing")
    elif kind == "formula":
        block = Block("b", "formula", formula=FormulaContent(asset_id="missing"))
    elif kind == "inline_formula":
        block = Block(
            "b",
            "paragraph",
            rich_text=RichText((InlineFormula(FormulaContent(asset_id="missing")),)),
        )
    else:
        block = Block(
            "b", "table", table=TableContent(representation="image", asset_id="missing")
        )
    with pytest.raises(ValueError, match="missing block asset"):
        replace(_text_unit(), blocks=(block,))


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_child",
        "wrong_parent",
        "duplicate_child",
        "unowned_child",
        "reverse_order",
        "duplicate_block",
        "parent_cycle",
    ],
)
def test_unit_requires_self_contained_table_children(corruption: str) -> None:
    child = Block(
        "cell",
        "paragraph",
        parent_block_id="table",
        rich_text=RichText((TextRun("actual"),)),
    )
    table = Block(
        "table", "table", table=TableContent(cells=(TableCell(0, 0, ("cell",)),))
    )
    blocks = (table, child)
    if corruption == "missing_child":
        blocks = (table,)
    elif corruption == "wrong_parent":
        blocks = (table, replace(child, parent_block_id="other"))
    elif corruption == "duplicate_child":
        blocks = (
            replace(
                table,
                table=TableContent(
                    cells=(TableCell(0, 0, ("cell",)), TableCell(0, 1, ("cell",)))
                ),
            ),
            child,
        )
    elif corruption == "unowned_child":
        blocks = (replace(table, table=TableContent(cells=())), child)
    elif corruption == "reverse_order":
        other = replace(child, block_id="second")
        blocks = (
            replace(
                table, table=TableContent(cells=(TableCell(0, 0, ("second", "cell")),))
            ),
            child,
            other,
        )
    elif corruption == "duplicate_block":
        blocks = (child, child)
    elif corruption == "parent_cycle":
        blocks = (replace(child, parent_block_id="cell"),)
    with pytest.raises(ValueError):
        replace(_text_unit(), blocks=blocks)
    restored = from_dict(to_dict(replace(_text_unit(), blocks=(table, child))))
    assert isinstance(restored, IndexUnit) and restored.blocks == (table, child)


@pytest.mark.parametrize(
    "corruption", ["duplicate", "missing_provenance", "self_provenance", "cycle"]
)
def test_unit_and_representation_require_complete_asset_provenance(
    corruption: str,
) -> None:
    first = asset("first")
    if corruption == "duplicate":
        assets = (first, first)
    elif corruption == "missing_provenance":
        assets = (replace(first, source_asset_ids=("missing",)),)
    elif corruption == "self_provenance":
        assets = (replace(first, source_asset_ids=(first.asset_id,)),)
    else:
        assets = (
            replace(first, source_asset_ids=("second",)),
            asset("second", sources=(first.asset_id,)),
        )
    with pytest.raises(ValueError, match="asset"):
        replace(_text_unit(), assets=assets)
    with pytest.raises(ValueError, match="asset"):
        replace(_text_representation(), assets=assets)
    valid = (asset("derived", sources=(first.asset_id,)), first)
    assert from_dict(to_dict(replace(_text_unit(), assets=valid))).assets == valid


def test_indexing_codec_rejects_dangling_materialized_content_without_relying_on_ids() -> (
    None
):
    child = Block(
        "cell",
        "paragraph",
        parent_block_id="table",
        rich_text=RichText((TextRun("actual"),)),
    )
    table = Block(
        "table", "table", table=TableContent(cells=(TableCell(0, 0, ("cell",)),))
    )
    unit = replace(_text_unit(), blocks=(table, child))
    data = to_dict(unit)
    data["blocks"] = data["blocks"][:1]
    with pytest.raises(ValueError, match="table cell"):
        from_dict(data)
    data = to_dict(replace(_text_unit(), assets=(asset("original"),)))
    data["assets"].append(deepcopy(data["assets"][0]))
    with pytest.raises(ValueError, match="duplicate asset"):
        from_dict(data)
