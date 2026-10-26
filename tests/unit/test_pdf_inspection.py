from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from comet_rag.ports import DocumentBlock, NormalizedDocument
from evals.pdf.data import load as load_reference
from evals.pdf.inspect import evidence_alignment, inspect, load_native, table_integrity
from evals.pdf.tables import tables
from evals.retrieval.data import quote_locations
from tests.fixtures.native_pdf import artifact


def test_native_artifact_identity_ignores_latency_but_keeps_canonical_and_page_checks(
    tmp_path: Path,
) -> None:
    data, native = artifact(tmp_path)
    documents, first = load_native(native, data=data)
    raw = json.loads((native / "report.json").read_text())
    raw["samples"][0]["elapsed_seconds"] = 9.9
    (native / "report.json").write_text(json.dumps(raw))
    _, second = load_native(native, data=data)
    assert first == second
    assert documents["nist145"].markdown == "A"
    assert evidence_alignment(documents, data=data) == [
        {
            "evidence_id": "e1",
            "document_id": "nist145",
            "expected_physical_page": 1,
            "match_count_lower_bound": 1,
            "alignment": "exact_on_expected_page",
            "native_span": [0, 1],
            "independent_review": "pending",
        }
    ]


@pytest.mark.parametrize(
    "fault",
    [
        "lock",
        "duplicate",
        "missing",
        "source",
        "canonical",
        "numbers",
        "float_protocol",
        "body",
        "rebuild",
        "page",
    ],
)
def test_native_audit_refuses_inconsistent_or_forged_artifacts(
    tmp_path: Path, fault: str
) -> None:
    data, native = artifact(tmp_path)
    report = json.loads((native / "report.json").read_text())
    sample = report["samples"][0]
    if fault == "lock":
        report["snapshot_sha256"] = "0" * 64
    elif fault == "duplicate":
        report["samples"].append(sample)
    elif fault == "missing":
        report["samples"].pop()
    elif fault == "source":
        sample["source_sha256"] = "0" * 64
    elif fault == "canonical":
        sample["normalized_sha256"] = "0" * 64
    elif fault == "numbers":
        sample["page_numbers"] = [True]
    elif fault == "float_protocol":
        sample["provider"]["protocol_version"] = 2.0
    else:
        output = json.loads((native / "nist145.json").read_text())
        if fault == "body":
            output["markdown"] = output["pages"][0]["markdown"] = "C"
        elif fault == "rebuild":
            output["pages"][0]["markdown"] = "Wrong"
        else:
            output["pages"][0]["page_number"] = 2
        (native / "nist145.json").write_text(json.dumps(output))
    (native / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        load_native(native, data=data)


@pytest.mark.parametrize("fault", ["duplicate", "wrong_page"])
def test_exact_quote_still_requires_unique_match_on_expected_page(
    tmp_path: Path, fault: str
) -> None:
    data, native = artifact(tmp_path)
    documents, _ = load_native(native, data=data)
    text = "AA" if fault == "duplicate" else "A"
    documents["nist145"] = NormalizedDocument(
        text,
        blocks=(
            DocumentBlock(
                "page", 0, 0, len(text), page_number=2 if fault == "wrong_page" else 1
            ),
        ),
    )
    row = evidence_alignment(documents, data=data)[0]
    assert row["alignment"] == "needs_review" and row["native_span"] is None
    assert row["independent_review"] == "pending"


def test_alignment_mismatch_never_invents_span_or_independent_approval(
    tmp_path: Path,
) -> None:
    data, native = artifact(tmp_path)
    documents, _ = load_native(native, data=data)
    documents["nist145"] = NormalizedDocument(
        "other", blocks=(DocumentBlock("page", 0, 0, 5, page_number=1),)
    )
    row = evidence_alignment(documents, data=data)[0]
    assert row["alignment"] == "needs_review" and row["native_span"] is None
    assert row["independent_review"] == "pending"


@pytest.mark.parametrize(
    "text,quote,expected",
    [
        ("x", "a", ()),
        ("abc", "b", (1,)),
        ("aaaa", "aaa", (0, 1)),
        ("a a a", "a", (0, 2)),
    ],
)
def test_quote_uniqueness_counts_overlapping_matches(text, quote, expected) -> None:
    assert quote_locations(text, quote) == expected


def test_reference_labels_reject_overlapping_ambiguous_quotes(tmp_path: Path) -> None:
    data, _ = artifact(tmp_path)
    snapshot = json.loads((data / "pages.json").read_text())
    snapshot["samples"][0]["pages"][0]["text"] = "aaaa"
    labels = json.loads((data / "judgments.json").read_text())
    labels["evidence"][0]["quote"] = "aaa"
    (data / "pages.json").write_text(json.dumps(snapshot))
    (data / "judgments.json").write_text(json.dumps(labels))
    with pytest.raises(ValueError, match="唯一"):
        load_reference(data)


def test_html_table_ranges_are_lexical_and_ignore_fenced_examples() -> None:
    row = "<tr><td>A</td></tr>"
    text = (
        '```html\n<table><tr><td>example</td></tr></table>\n```\n\n<table data-x=">">'
        + row
        + row
        + "</table>"
    )
    result = tables(text)
    assert len(result) == 1
    table = result[0]
    assert (
        text[table.start : table.end] == '<table data-x=">">' + row + row + "</table>"
    )
    assert len(table.rows) == 2 and table.rows[0][0] < table.rows[1][0]
    assert [text[a:b] for a, b in table.rows] == [row, row]


@pytest.mark.parametrize(
    "text",
    [
        "<table><tr><td>x</td></tr>",
        "<table><tr><td>x</td></table>",
        "<table></table>",
        "<table>\n<table><tr><td>x</td></tr></table></table>",
    ],
)
def test_unassessable_html_tables_fail_instead_of_reporting_zero_complete_rows(
    text: str,
) -> None:
    with pytest.raises(ValueError):
        tables(text)


def test_table_integrity_distinguishes_full_table_rows_and_hard_budget_limits() -> None:
    text = "<table><tr><td>" + "A" * 900 + "</td></tr><tr><td>B</td></tr></table>"
    doc = NormalizedDocument(
        text, blocks=(DocumentBlock("page", 0, 0, len(text), page_number=1),)
    )
    report = table_integrity(doc)[0]
    assert report["rows"] == 2 and report["rows_over_child_budget"] == 1
    assert report["page_child"] == {"whole_table": False, "whole_rows": 1}
    assert report["parent"] == {"whole_table": True, "whole_rows": 2}


def test_inspection_is_diagnostic_and_preserves_new_output_directory_guard(
    tmp_path: Path,
) -> None:
    data, native = artifact(tmp_path)
    out = tmp_path / "audit"
    inspect(native, out, data=data)
    report = json.loads((out / "audit.json").read_text())
    assert (
        report["independent_review"] == "pending"
        and report["fixed_weights"] == "unknown"
    )
    assert "not quality acceptance" in report["scope"]
    with pytest.raises(FileExistsError):
        inspect(native, out, data=data)


def test_native_artifact_requires_all_physical_pages_even_if_report_is_consistent(
    tmp_path: Path,
) -> None:
    from comet_rag.ports import ExtractedDocument, ExtractedPage
    from evals.pdf.parse import page_summary

    data, native = artifact(tmp_path)
    path = data / "pages.json"
    snapshot = json.loads(path.read_text())
    snapshot["samples"][0]["total_pages"] = 3
    path.write_text(json.dumps(snapshot))
    output = {
        "markdown": "A\n\nC",
        "pages": [
            {"page_number": 1, "markdown": "A"},
            {"page_number": 3, "markdown": "C"},
        ],
    }
    (native / "nist145.json").write_text(json.dumps(output))
    report = json.loads((native / "report.json").read_text())
    report["snapshot_sha256"] = sha256(path.read_bytes()).hexdigest()
    report["samples"][0].update(
        page_summary(
            ExtractedDocument(
                "A\n\nC", pages=(ExtractedPage(1, "A"), ExtractedPage(3, "C"))
            ),
            3,
        )
    )
    (native / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="完整"):
        load_native(native, data=data)
