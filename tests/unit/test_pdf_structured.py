from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from evals.pdf.structured import StructuredLimits, project_structured

SOURCE = "a" * 64
BBOX = [0.1, 0.1, 0.8, 0.3]


def block(kind: str, text: str, **extra: object) -> dict[str, object]:
    return {"type": kind, "bbox": BBOX, "content": text, **extra}


def fixture() -> dict:
    # dict仅作可变故障载体，便于注入同一字段的错误JSON类型。
    return {
        "metadata": {
            "file_suffix": "pdf",
            "producer": {"name": "mineru", "version": "4.0.12"},
            "document": {"page_count": 3, "page_count_kind": "physical"},
        },
        "is_full_document": True,
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    block("doc_title", "Report", level=1),
                    block("paragraph_title", "Scope", level=2),
                    block("text", "Same"),
                    block(
                        "image",
                        "Estimated 99",
                        captions=[{"content": "Figure one"}],
                        footnotes=[],
                        image_source="data:image/png;base64,AAAA",
                    ),
                    block(
                        "table",
                        "<table><tr><td>Year</td><td>Count</td></tr><tr><td>2024</td><td>42</td></tr></table>",
                        captions=[{"content": "Observed counts"}],
                        footnotes=[{"content": "Source note"}],
                    ),
                ],
            },
            {"page_idx": 1, "blocks": []},
            {
                "page_idx": 2,
                "blocks": [
                    block("text", "Same", continues_prev=True),
                    block("paragraph_title", "Deep", level=4),
                    block("paragraph_title", "Next", level=3),
                    block("footer", "Back cover"),
                ],
            },
        ],
    }


def project(payload: dict | None = None, **kwargs):
    # 实验fixture无生产类型契约；kwargs用于对独立预算注入故障。
    return project_structured(
        json.dumps(fixture() if payload is None else payload).encode(),
        source_sha256=SOURCE,
        expected_pages=3,
        **kwargs,
    )


def test_literal_projection_keeps_pages_titles_table_and_notes() -> None:
    result = project()
    expected_first = (
        "Report\n\nScope\n\nSame\n\nFigure one\n\n"
        "<table><tr><td>Year</td><td>Count</td></tr><tr><td>2024</td><td>42</td></tr></table>"
        "\n\nObserved counts\n\nSource note"
    )
    assert [p.markdown for p in result.document.pages] == [
        expected_first,
        "",
        "Same\n\nDeep\n\nNext\n\nBack cover",
    ]
    assert (
        result.document.markdown
        == expected_first + "\n\nSame\n\nDeep\n\nNext\n\nBack cover"
    )
    assert [p.page_number for p in result.document.pages] == [1, 2, 3]
    assert "Estimated 99" not in result.document.markdown
    assert [i.text for i in result.interpretations] == ["Estimated 99"]
    assert result.assets[0].locator_bytes == 26


def test_heading_path_crosses_page_without_moving_body_or_filling_levels() -> None:
    result = project()
    third = [s for s in result.evidence if s.page_number == 3]
    assert [s.heading_path for s in third] == [
        ("Report", "Scope"),
        ("Report", "Scope", "Deep"),
        ("Report", "Scope", "Next"),
        (),
    ]
    assert third[0].continues_prev
    assert result.document.markdown[third[0].start_char : third[0].end_char] == "Same"


def test_table_related_parts_have_one_parent_and_distinct_exact_spans() -> None:
    result = project()
    parts = [s for s in result.evidence if s.block_type == "table"]
    assert [s.role for s in parts] == ["body", "captions", "footnotes"]
    assert {s.parent_pointer for s in parts} == {"/pages/0/blocks/4"}
    for span in parts:
        result.verify_quote(
            pointer=span.pointer,
            canonical_sha256=result.canonical_sha256,
            start_char=span.start_char,
            end_char=span.end_char,
            quote=result.document.markdown[span.start_char : span.end_char],
        )
    assert parts[1].bbox is None  # 无子块坐标时，不借父块坐标冒充。


@pytest.mark.parametrize(
    "fault",
    [
        "old_hash",
        "cross_page",
        "derived",
        "auxiliary",
        "wrong_quote",
        "bool_offset",
        "truthy_auxiliary",
    ],
)
def test_quote_rejects_unqualified_or_stale_evidence(fault: str) -> None:
    result = project()
    first, last = result.evidence[0], result.evidence[-1]
    # Any仅用于对正式签名注入错误类型，不放宽实现的参数契约。
    args: dict[str, Any] = {
        "pointer": first.pointer,
        "canonical_sha256": result.canonical_sha256,
        "start_char": first.start_char,
        "end_char": first.end_char,
        "quote": "Report",
    }
    if fault == "old_hash":
        args["canonical_sha256"] = "0" * 64
    elif fault == "cross_page":
        args["end_char"] = last.end_char
        args["quote"] = result.document.markdown
    elif fault == "derived":
        args["pointer"] = result.interpretations[0].pointer
    elif fault == "auxiliary":
        args.update(
            pointer=last.pointer,
            start_char=last.start_char,
            end_char=last.end_char,
            quote="Back cover",
        )
    elif fault == "wrong_quote":
        args["quote"] = "report"
    elif fault == "bool_offset":
        args["start_char"] = False
    else:
        args["allow_auxiliary"] = 1
    with pytest.raises(ValueError):
        result.verify_quote(**args)


def test_auxiliary_requires_explicit_permission_but_is_not_silently_lost() -> None:
    result = project()
    span = result.evidence[-1]
    assert span.category == "auxiliary"
    assert span.heading_path == ()
    result.verify_quote(
        pointer=span.pointer,
        canonical_sha256=result.canonical_sha256,
        start_char=span.start_char,
        end_char=span.end_char,
        quote="Back cover",
        allow_auxiliary=True,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "bool_page",
        "duplicate_page",
        "missing_page",
        "wrong_count",
        "bool_count",
        "wrong_producer",
        "middle_json",
        "new_type",
        "bad_bbox",
        "bool_level",
        "bad_level",
        "bad_annotation",
        "wrong_continuation",
    ],
)
def test_structured_mapping_rejects_uncertain_facts(fault: str) -> None:
    raw = fixture()
    if fault == "bool_page":
        raw["pages"][0]["page_idx"] = False
    elif fault == "duplicate_page":
        raw["pages"][1]["page_idx"] = 0
    elif fault == "missing_page":
        del raw["pages"][1]
    elif fault == "wrong_count":
        raw["metadata"]["document"]["page_count"] = 4
    elif fault == "bool_count":
        raw["metadata"]["document"]["page_count"] = True
    elif fault == "wrong_producer":
        raw["metadata"]["producer"]["version"] = "4.0.13"
    elif fault == "middle_json":
        raw["schema"] = "docvortex.middle"
    elif fault == "new_type":
        raw["pages"][0]["blocks"][0]["type"] = "unknown"
    elif fault == "bad_bbox":
        raw["pages"][0]["blocks"][0]["bbox"] = [0, 0, True, 1]
    elif fault == "bool_level":
        raw["pages"][0]["blocks"][0]["level"] = True
    elif fault == "bad_level":
        raw["pages"][0]["blocks"][1]["level"] = 1
    elif fault == "bad_annotation":
        raw["pages"][0]["blocks"][3]["captions"][0]["content"] = 4
    else:
        raw["pages"][2]["blocks"][0]["continues_prev"] = 1
    with pytest.raises(ValueError):
        project(raw)


@pytest.mark.parametrize(
    "field",
    [
        "wire_bytes",
        "text_bytes",
        "interpretation_bytes",
        "block_bytes",
        "pages",
        "blocks",
        "annotations_per_group",
        "evidence_units",
    ],
)
def test_each_budget_rejects_overflow(field: str) -> None:
    limits = replace(StructuredLimits(), **{field: 1})
    raw = fixture()
    if field == "annotations_per_group":
        raw["pages"][0]["blocks"][3]["captions"].append({"content": "Second"})
    with pytest.raises(ValueError):
        project(raw, limits=limits)


@pytest.mark.parametrize(
    "raw", [b'{"pages":[],"pages":[]}', b'{"x":NaN}', b'{"x":1e999}', b"\xff", b"[]"]
)
def test_json_rejects_duplicate_keys_nonfinite_or_wrong_encoding(raw: bytes) -> None:
    with pytest.raises(ValueError):
        project_structured(raw, source_sha256=SOURCE, expected_pages=3)


def test_identity_binds_structure_and_classification_but_not_ignored_asset_bytes() -> (
    None
):
    original = project()
    raw = fixture()
    raw["pages"][0]["blocks"][3]["image_source"] = "https://unvisited.invalid/image.png"
    changed_asset = project(raw)
    assert changed_asset.canonical_sha256 == original.canonical_sha256
    assert changed_asset.input_sha256 != original.input_sha256
    raw["pages"][2]["blocks"][-1]["type"] = "text"
    reclassified = project(raw)
    assert reclassified.document.markdown == original.document.markdown
    assert reclassified.canonical_sha256 != original.canonical_sha256


def test_rendered_body_does_not_claim_heading_structure_survives_legacy_port() -> None:
    from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer

    result = project()
    normalized = MarkdownDocumentNormalizer().normalize(result.document)
    assert [b.heading_path for b in normalized.blocks] == [(), ()]
    assert any(s.heading_path == ("Report", "Scope") for s in result.evidence)


def test_exact_wire_boundary_and_utf8_character_spans() -> None:
    raw = fixture()
    raw["pages"][0]["blocks"][2]["content"] = "e\u0301"
    wire = json.dumps(raw).encode()
    result = project_structured(
        wire,
        source_sha256=SOURCE,
        expected_pages=3,
        limits=replace(StructuredLimits(), wire_bytes=len(wire)),
    )
    span = next(s for s in result.evidence if s.pointer == "/pages/0/blocks/2/content")
    assert result.document.markdown[span.start_char : span.end_char] == "é"
    assert span.end_char - span.start_char == 1
    with pytest.raises(ValueError, match="wire"):
        project_structured(
            wire,
            source_sha256=SOURCE,
            expected_pages=3,
            limits=replace(StructuredLimits(), wire_bytes=len(wire) - 1),
        )


def test_source_page_without_text_keeps_asset_and_interpretation_identity() -> None:
    raw = fixture()
    raw["pages"][1]["blocks"] = [
        block(
            "image",
            "A night scene",
            captions=[],
            footnotes=[],
            image_source="image.png",
        )
    ]
    result = project(raw)
    assert result.document.pages[1].page_number == 2
    assert result.document.pages[1].markdown == ""
    assert result.interpretations[-1].page_number == 2
    assert result.assets[-1].page_number == 2


def test_heading_stack_uses_levels_not_stack_depth() -> None:
    raw = fixture()
    raw["pages"][0]["blocks"] = [
        block("doc_title", "Report", level=1),
        block("paragraph_title", "Deep", level=4),
        block("paragraph_title", "Next", level=3),
    ]
    result = project(raw)
    assert result.evidence[2].heading_path == ("Report", "Next")


def test_extraction_category_is_not_independent_validation_of_model_table_labels() -> (
    None
):
    raw = fixture()
    raw["pages"][0]["blocks"][4]["content"] = (
        "| Year | Estimate |\n| --- | --- |\n| 2024 | ~99 |"
    )
    result = project(raw)
    span = next(s for s in result.evidence if s.pointer == "/pages/0/blocks/4/content")
    assert span.category == "extraction"
    assert "~99" in result.document.markdown[span.start_char : span.end_char]


@pytest.mark.parametrize("fault", ["missing_page", "no_source_text"])
def test_bad_mapping_never_writes_a_successful_summary(tmp_path, fault: str) -> None:
    from hashlib import sha256

    from evals.pdf.structured_audit import audit

    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"locked fixture bytes; never sent to a parser")
    lock = tmp_path / "inputs.json"
    lock.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "id": "sample",
                        "path": str(pdf),
                        "pages": 3,
                        "bytes": pdf.stat().st_size,
                        "sha256": sha256(pdf.read_bytes()).hexdigest(),
                    }
                ]
            }
        )
    )
    invalid = fixture()
    if fault == "missing_page":
        invalid["pages"].pop()
    else:
        for page in invalid["pages"]:
            page["blocks"] = []
    source = tmp_path / "structured.json"
    source.write_text(json.dumps(invalid))
    output = tmp_path / "new"
    with pytest.raises(ValueError, match="物理页|空正文"):
        audit(source, lock, "sample", output)
    assert not output.exists()


def test_limited_reader_checks_actual_bytes_when_file_grows_after_stat() -> None:
    from io import BytesIO
    from pathlib import Path
    from types import SimpleNamespace
    from typing import cast

    from evals.pdf.structured_audit import read_limited

    class GrowingFile(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size == 4
            return super().read(size)

    path = SimpleNamespace(
        stat=lambda: SimpleNamespace(st_size=1), open=lambda _: GrowingFile(b"abcdef")
    )
    with pytest.raises(ValueError, match="wire"):
        read_limited(cast(Path, path), 3)
