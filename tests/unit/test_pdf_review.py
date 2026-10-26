from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.pdf.artifacts import blind_packet, freeze
from evals.pdf.review import validate_review
from tests.fixtures.native_pdf import artifact


def submission(tmp_path: Path):
    data, native = artifact(tmp_path)
    frozen, packet = tmp_path / "frozen", tmp_path / "packet"
    freeze(
        native,
        frozen,
        data=data,
        attribution="Synthetic fixture; local evaluation only",
    )
    blind_packet(frozen, packet, data=data)
    review = json.loads((packet / "review.json").read_text())
    review.update(
        status="submitted",
        reviewer="fixture reviewer",
        reviewed_at="2026-10-26",
        independence_basis="fixture only; no actual human review",
    )
    review["decisions"] = [
        {
            "query_id": "q1",
            "decision": "answerable",
            "relevance": {"nist145": 2},
            "evidence": [
                {
                    "document_id": "nist145",
                    "page_number": 1,
                    "start_char": 0,
                    "end_char": 1,
                    "quote": "A",
                    "role": "passage",
                }
            ],
            "notes": "",
        }
    ]
    path = tmp_path / "response.json"
    path.write_text(json.dumps(review))
    return data, frozen, packet, path, review


def test_structurally_complete_review_never_certifies_human_independence(
    tmp_path: Path,
) -> None:
    data, frozen, packet, path, _ = submission(tmp_path)
    result = validate_review(packet, path, frozen=frozen, data=data)
    assert result["ready_for_adjudication"] is True
    assert result["independent_review"] == "unverified"
    assert result["quality_acceptance"] == "pending"


@pytest.mark.parametrize(
    "fault",
    [
        "scope",
        "canonical",
        "missing",
        "duplicate",
        "unknown_question",
        "unknown_source",
        "empty_evidence",
        "no_positive",
        "no_answer_evidence",
        "no_answer_notes",
        "uncertain_notes",
        "quote",
        "page",
        "span",
        "bool_page",
        "bool_grade",
        "duplicate_evidence",
        "joined_without_notes",
        "blank_reviewer",
        "bad_date",
        "auto_approved",
        "noncanonical_date",
    ],
)
def test_invalid_review_cannot_be_treated_as_complete(
    tmp_path: Path, fault: str
) -> None:
    data, frozen, packet, path, raw = submission(tmp_path)
    decision = raw["decisions"][0]
    fragment = decision["evidence"][0]
    if fault == "scope":
        raw["review_scope_sha256"] = "0" * 64
    elif fault == "canonical":
        raw["full_canonical_sha256"] = "0" * 64
    elif fault == "missing":
        raw["decisions"] = []
    elif fault == "duplicate":
        raw["decisions"].append(decision)
    elif fault == "unknown_question":
        decision["query_id"] = "unknown"
    elif fault == "unknown_source":
        decision["relevance"]["unknown"] = 1
    elif fault == "empty_evidence":
        decision["evidence"] = []
    elif fault == "no_positive":
        decision["relevance"] = {}
    elif fault == "no_answer_evidence":
        decision.update(decision="unanswerable", notes="checked")
    elif fault == "no_answer_notes":
        decision.update(decision="unanswerable", relevance={}, evidence=[])
    elif fault == "uncertain_notes":
        decision["decision"] = "uncertain"
    elif fault == "quote":
        fragment["quote"] = "B"
    elif fault == "page":
        fragment["page_number"] = 2
    elif fault == "span":
        fragment["end_char"] = 2
    elif fault == "bool_page":
        fragment["page_number"] = True
    elif fault == "bool_grade":
        decision["relevance"]["nist145"] = True
    elif fault == "duplicate_evidence":
        decision["evidence"].append(fragment)
        decision["notes"] = "two copies"
    elif fault == "joined_without_notes":
        decision["relevance"]["nist144"] = 1
        decision["evidence"].append(
            {**fragment, "document_id": "nist144", "quote": "B"}
        )
    elif fault == "blank_reviewer":
        raw["reviewer"] = " "
    elif fault == "bad_date":
        raw["reviewed_at"] = "2026-02-30"
    elif fault == "noncanonical_date":
        raw["reviewed_at"] = "20261026"
    else:
        raw["status"] = "approved"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        validate_review(packet, path, frozen=frozen, data=data)


@pytest.mark.parametrize("file", ["questions.json", "sources.json"])
def test_review_inputs_must_match_regenerated_frozen_scope(
    tmp_path: Path, file: str
) -> None:
    data, frozen, packet, path, _ = submission(tmp_path)
    raw = json.loads((packet / file).read_text())
    if file == "questions.json":
        raw[0]["question"] = "changed question"
    else:
        raw["sources"][0]["pages"][0]["start_char"] = False
    (packet / file).write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="冻结输入"):
        validate_review(packet, path, frozen=frozen, data=data)


@pytest.mark.parametrize("decision", ["unanswerable", "uncertain"])
def test_explicit_negative_and_uncertain_responses_remain_distinct(
    tmp_path: Path, decision: str
) -> None:
    data, frozen, packet, path, raw = submission(tmp_path)
    raw["decisions"][0].update(
        decision=decision,
        relevance={},
        evidence=[],
        notes="fixture scope checked; unresolved issue if uncertain",
    )
    path.write_text(json.dumps(raw))
    result = validate_review(packet, path, frozen=frozen, data=data)
    assert result["unresolved_queries"] == (["q1"] if decision == "uncertain" else [])
    assert result["ready_for_adjudication"] is (decision != "uncertain")
    assert result["quality_acceptance"] == "pending"


def test_response_hash_identifies_validated_bytes_not_a_later_file_version(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import date
    from hashlib import sha256

    import evals.pdf.review as module

    data, frozen, packet, path, _ = submission(tmp_path)
    expected = sha256(path.read_bytes()).hexdigest()

    class ConcurrentWrite:
        @staticmethod
        def fromisoformat(value: str):
            result = date.fromisoformat(value)
            path.write_text("injected unvalidated response")
            return result

    monkeypatch.setattr(module, "date", ConcurrentWrite)
    report = validate_review(packet, path, frozen=frozen, data=data)
    assert report["response_sha256"] == expected


def test_duplicate_response_keys_are_rejected_before_adjudication(
    tmp_path: Path,
) -> None:
    data, frozen, packet, path, _ = submission(tmp_path)
    path.write_text(
        path.read_text().replace(
            '"status": "submitted"', '"status": "submitted", "status": "submitted"'
        )
    )
    with pytest.raises(ValueError, match="重复 JSON 键"):
        validate_review(packet, path, frozen=frozen, data=data)


def test_joint_fragments_with_explanation_remain_separate_evidence(
    tmp_path: Path,
) -> None:
    data, frozen, packet, path, raw = submission(tmp_path)
    decision = raw["decisions"][0]
    decision["relevance"]["nist144"] = 1
    decision["evidence"].append(
        {**decision["evidence"][0], "document_id": "nist144", "quote": "B"}
    )
    decision["notes"] = (
        "Fixture requires both literal fragments; not a continuous quote."
    )
    path.write_text(json.dumps(raw))
    report = validate_review(packet, path, frozen=frozen, data=data)
    assert report["ready_for_adjudication"] is True
    assert report["quality_acceptance"] == "pending"


def test_evidence_cannot_cross_the_query_knowledge_base(tmp_path: Path) -> None:
    from hashlib import sha256

    data, native = artifact(tmp_path)
    snapshot = json.loads((data / "pages.json").read_text())
    snapshot["samples"][1]["source"]["kb_id"] = "other"
    (data / "pages.json").write_text(json.dumps(snapshot))
    report = json.loads((native / "report.json").read_text())
    report["snapshot_sha256"] = sha256((data / "pages.json").read_bytes()).hexdigest()
    (native / "report.json").write_text(json.dumps(report))
    frozen, packet = tmp_path / "frozen", tmp_path / "packet"
    freeze(
        native,
        frozen,
        data=data,
        attribution="Synthetic fixture; local evaluation only",
    )
    blind_packet(frozen, packet, data=data)
    raw = json.loads((packet / "review.json").read_text())
    raw.update(
        status="submitted",
        reviewer="fixture",
        reviewed_at="2026-10-26",
        independence_basis="fixture only",
    )
    raw["decisions"] = [
        {
            "query_id": "q1",
            "decision": "answerable",
            "relevance": {"nist144": 2},
            "evidence": [
                {
                    "document_id": "nist144",
                    "page_number": 1,
                    "start_char": 0,
                    "end_char": 1,
                    "quote": "B",
                    "role": "passage",
                }
            ],
            "notes": "",
        }
    ]
    path = tmp_path / "response.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="跨知识库"):
        validate_review(packet, path, frozen=frozen, data=data)


@pytest.mark.parametrize("quote", ["", " ", "\n\t"])
def test_evidence_fragment_cannot_be_only_whitespace(quote: str) -> None:
    from evals.pdf.review import Fragment

    with pytest.raises(ValueError):
        Fragment(
            document_id="nist145",
            page_number=1,
            start_char=0,
            end_char=1,
            quote=quote,
            role="passage",
        )
