from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.pdf.artifacts import blind_packet, freeze, load_frozen
from tests.fixtures.native_pdf import artifact


def test_freeze_is_byte_preserving_and_blind_packet_has_no_answer_leak(
    tmp_path: Path,
) -> None:
    data, native = artifact(tmp_path)
    frozen = tmp_path / "frozen"
    freeze(
        native,
        frozen,
        data=data,
        attribution="Synthetic fixture; local evaluation only",
    )
    documents, digest = load_frozen(frozen, data=data)
    assert documents["nist145"].markdown == "A"
    assert (frozen / "nist145.json").read_bytes() == (
        native / "nist145.json"
    ).read_bytes()
    out = tmp_path / "blind"
    blind_packet(frozen, out, data=data)
    questions = json.loads((out / "questions.json").read_text())
    assert questions == [{"query_id": "q1", "kb_id": "kb", "question": "question"}]
    review = json.loads((out / "review.json").read_text())
    assert review["full_canonical_sha256"] == digest
    assert (
        review["status"] == "pending"
        and review["reviewer"] == review["independence_basis"] == ""
    )
    assert review["decisions"] == [
        {
            "query_id": "q1",
            "decision": "unreviewed",
            "relevance": {},
            "evidence": [],
            "notes": "",
        }
    ]
    sources = json.loads((out / "sources.json").read_text())
    assert sources["attribution"] == "Synthetic fixture; local evaluation only"
    assert "Synthetic fixture; local evaluation only" in (out / "README.md").read_text()
    assert set(sources) == {"attribution", "full_canonical_sha256", "sources"}
    assert sources["sources"][0]["pages"] == [
        {"page_number": 1, "start_char": 0, "end_char": 1, "markdown": "A"}
    ]
    assert {p.name for p in out.iterdir()} == {
        "README.md",
        "sources.json",
        "questions.json",
        "review.json",
    }
    with pytest.raises(FileExistsError):
        freeze(
            native,
            frozen,
            data=data,
            attribution="Synthetic fixture; local evaluation only",
        )
    with pytest.raises(FileExistsError):
        blind_packet(frozen, out, data=data)


@pytest.mark.parametrize(
    "fault", ["bytes", "canonical", "missing", "traversal", "bool_version"]
)
def test_frozen_corpus_rejects_changes_before_exporting_review(
    tmp_path: Path, fault: str
) -> None:
    data, native = artifact(tmp_path)
    frozen = tmp_path / "frozen"
    freeze(
        native,
        frozen,
        data=data,
        attribution="Synthetic fixture; local evaluation only",
    )
    path = frozen / "manifest.json"
    manifest = json.loads(path.read_text())
    if fault == "bytes":
        path_to_body = frozen / "nist145.json"
        path_to_body.write_bytes(path_to_body.read_bytes() + b"\n ")
    elif fault == "canonical":
        manifest["canonical_sha256"] = "0" * 64
    elif fault == "missing":
        manifest["files"].pop("nist145.json")
    elif fault == "traversal":
        manifest["files"]["../nist145.json"] = manifest["files"].pop("nist145.json")
    else:
        manifest["version"] = True
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        blind_packet(frozen, tmp_path / "review", data=data)
    assert not (tmp_path / "review").exists()


def test_failed_freeze_has_no_valid_manifest(tmp_path: Path, monkeypatch) -> None:
    data, native = artifact(tmp_path)
    write = Path.write_bytes

    def fail(path: Path, raw: bytes) -> int:
        if path.parent.name == "frozen" and path.name == "nist144.json":
            raise OSError("injected write failure")
        return write(path, raw)

    monkeypatch.setattr(Path, "write_bytes", fail)
    frozen = tmp_path / "frozen"
    with pytest.raises(OSError, match="injected"):
        freeze(
            native,
            frozen,
            data=data,
            attribution="Synthetic fixture; local evaluation only",
        )
    assert not (frozen / "manifest.json").exists()
    with pytest.raises(FileNotFoundError):
        load_frozen(frozen, data=data)


def test_committed_rounds_have_distinct_content_identity_and_exact_page_scope(
    tmp_path: Path,
) -> None:
    from evals.pdf.data import DATA

    rounds = {}
    for name, expected in [
        (
            "20261025-b29",
            "4048da0485907f0b23892cb4c7e8758c704d51dff490777045dbb0bede567651",
        ),
        (
            "20261025-b30",
            "a5ef5b260b3472d8a8e0dc5541c8de24053bbd42cc70f0f06e0e643f02f7a1d8",
        ),
    ]:
        documents, digest = load_frozen(DATA / "native" / name)
        assert digest == expected
        assert [b.page_number for b in documents["nist145"].blocks] == list(range(1, 8))
        assert [b.page_number for b in documents["nist144"].blocks] == list(
            range(1, 81)
        )
        rounds[name] = digest
    assert len(set(rounds.values())) == 2
    blind_packet(DATA / "native/20261025-b30", tmp_path / "review")
    sources = json.loads((tmp_path / "review/sources.json").read_text())["sources"]
    assert [[p["page_number"] for p in source["pages"]] for source in sources] == [
        [5, 6, 7],
        [13, 14, 15, 16, 17, 18, 19, 20, 52],
    ]


@pytest.mark.parametrize("fault", ["duplicate", "unknown_kb"])
def test_blind_packet_refuses_ambiguous_question_scope(
    tmp_path: Path, fault: str
) -> None:
    data, native = artifact(tmp_path)
    frozen = tmp_path / "frozen"
    freeze(
        native,
        frozen,
        data=data,
        attribution="Synthetic fixture; local evaluation only",
    )
    path = data / "judgments.json"
    labels = json.loads(path.read_text())
    if fault == "duplicate":
        labels["queries"].append(labels["queries"][0])
    else:
        labels["queries"][0]["kb_id"] = "unknown"
    path.write_text(json.dumps(labels))
    with pytest.raises(ValueError):
        blind_packet(frozen, tmp_path / "review", data=data)
    assert not (tmp_path / "review").exists()


def test_freeze_rejects_consistent_source_change_between_validation_and_copy(
    tmp_path: Path, monkeypatch
) -> None:
    from hashlib import sha256

    data, native = artifact(tmp_path)
    read = Path.read_bytes
    changed = False

    def race(path: Path) -> bytes:
        nonlocal changed
        if path == native / "report.json" and not changed:
            changed = True
            output = json.loads((native / "nist145.json").read_text())
            output["markdown"] = output["pages"][0]["markdown"] = "C"
            (native / "nist145.json").write_text(json.dumps(output))
            report = json.loads(path.read_text())
            report["samples"][0]["normalized_sha256"] = sha256(b"C").hexdigest()
            path.write_text(json.dumps(report))
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", race)
    frozen = tmp_path / "frozen"
    with pytest.raises(ValueError, match="canonical 发生变化"):
        freeze(
            native,
            frozen,
            data=data,
            attribution="Synthetic fixture; local evaluation only",
        )
    assert changed and not (frozen / "manifest.json").exists()


def test_custom_source_must_declare_attribution_instead_of_inheriting_nist(
    tmp_path: Path,
) -> None:
    data, native = artifact(tmp_path)
    with pytest.raises(ValueError, match="归属"):
        freeze(native, tmp_path / "wrong-attribution", data=data)
    assert not (tmp_path / "wrong-attribution").exists()


def test_declared_empty_pages_must_match_normalized_content(tmp_path: Path) -> None:
    from evals.pdf.inspect import load_native

    data, native = artifact(tmp_path)
    report = json.loads((native / "report.json").read_text())
    report["samples"][0]["normalized_empty_page_numbers"] = [1]
    (native / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="normalized_empty_page_numbers"):
        load_native(native, data=data)
