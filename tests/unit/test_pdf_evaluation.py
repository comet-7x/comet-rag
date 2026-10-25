from __future__ import annotations

import json
from hashlib import sha256

import pytest

from evals.pdf.data import DATA, blind_packet, load
from evals.pdf.tokens import BodyTokens, body_budget
from evals.retrieval.data import generate
from evals.retrieval.metrics import Hit, QueryRun, Run


@pytest.fixture(scope="module")
def dataset():
    return load()


def test_pdf_snapshot_keeps_real_physical_pages_and_provisional_labels(dataset) -> None:
    assert (
        dataset.fingerprint
        == "19ff804c319c07ab01bc891abf2e971832e1a731047ea9be8330269bad6ba720"
    )
    assert [b.page_number for b in dataset.documents["nist145"].blocks] == [5, 6, 7]
    assert [b.page_number for b in dataset.documents["nist144"].blocks] == [
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        52,
    ]
    assert len(dataset.judgments.queries) == 24
    assert len(dataset.judgments.evidence) == 21
    assert sum(q.category == "no_answer" for q in dataset.judgments.queries) == 4
    query = next(q for q in dataset.judgments.queries if q.id == "q006")
    assert query.evidence == ["paas_begin", "paas_end"]
    for eid, page in [("paas_begin", 6), ("paas_end", 7)]:
        start, end = dataset.spans[eid]
        block = next(
            b for b in dataset.documents["nist145"].blocks if b.page_number == page
        )
        assert block.start_char <= start < end <= block.end_char
    assert not list(DATA.glob("*.pdf"))


def copy_data(tmp_path):
    for name in ("pages.json", "judgments.json"):
        (tmp_path / name).write_bytes((DATA / name).read_bytes())
    return tmp_path


@pytest.mark.parametrize(
    "fault", ["duplicate", "order", "overflow", "format", "generated"]
)
def test_corrupt_page_facts_fail_instead_of_fabricating_boundaries(
    tmp_path, fault
) -> None:
    copy_data(tmp_path)
    path = tmp_path / "pages.json"
    raw = json.loads(path.read_text())
    sample = raw["samples"][0]
    if fault == "duplicate":
        sample["pages"][1]["page_number"] = 5
    elif fault == "order":
        sample["pages"].reverse()
    elif fault == "overflow":
        sample["pages"][-1]["page_number"] = 8
    elif fault == "format":
        sample["source"]["format"] = "md"
        sample["source"]["sections"] = [{"heading": "fake", "paragraphs": ["fake"]}]
    else:
        sample["source"]["sections"] = [{"heading": "fake", "paragraphs": ["fake"]}]
    path.write_text(json.dumps(raw))
    with pytest.raises((ValueError, TypeError)):
        load(tmp_path)


def test_same_text_with_different_page_identity_changes_fingerprint(
    tmp_path, dataset
) -> None:
    copy_data(tmp_path)
    path = tmp_path / "pages.json"
    raw = json.loads(path.read_text())
    raw["samples"][1]["pages"][-1]["page_number"] = 53
    path.write_text(json.dumps(raw))
    assert load(tmp_path).fingerprint != dataset.fingerprint


def test_cross_page_answer_must_be_labeled_as_separate_evidence(
    tmp_path, dataset
) -> None:
    copy_data(tmp_path)
    page6, page7 = dataset.documents["nist145"].blocks[1:]
    quote = dataset.documents["nist145"].markdown[
        page6.end_char - 10 : page7.start_char + 10
    ]
    path = tmp_path / "judgments.json"
    labels = json.loads(path.read_text())
    labels["evidence"][0]["quote"] = quote
    path.write_text(json.dumps(labels))
    with pytest.raises(ValueError, match="每份证据必须完整位于一页"):
        load(tmp_path)


def test_pdf_cannot_be_generated_from_markdown_under_a_pdf_extension(tmp_path) -> None:
    from evals.pdf.data import PDFSnapshot
    from evals.retrieval.data import read_json

    snapshot = read_json(DATA / "pages.json", PDFSnapshot)
    (tmp_path / "corpus.json").write_text(
        json.dumps(
            {
                "version": 1,
                "documents": [s.source.model_dump() for s in snapshot.samples],
            }
        )
    )
    (tmp_path / "judgments.json").write_bytes((DATA / "judgments.json").read_bytes())
    with pytest.raises(ValueError, match="不能生成 Markdown 冒充 PDF"):
        generate(tmp_path / "output", data=tmp_path)
    assert not (tmp_path / "output").exists()


def test_blind_packet_has_no_gold_or_model_rankings_and_stays_pending(
    tmp_path, dataset
) -> None:
    output = tmp_path / "review"
    blind_packet(output)
    questions = json.loads((output / "questions.json").read_text())
    assert len(questions) == 24
    assert all(set(q) == {"query_id", "kb_id", "question"} for q in questions)
    review = json.loads((output / "review.json").read_text())
    assert review["dataset_sha256"] == dataset.fingerprint
    assert review["status"] == "pending" and review["reviewer"] == ""
    assert len(review["decisions"]) == 24
    assert all(
        d["relevance"] == {} and d["evidence"] == [] for d in review["decisions"]
    )
    assert {p.name for p in output.iterdir()} == {
        "README.md",
        "questions.json",
        "sources.json",
        "review.json",
    }
    assert load().fingerprint == dataset.fingerprint
    with pytest.raises(FileExistsError):
        blind_packet(output)


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"count": True, "tokens": [1]},
        {"count": -1, "tokens": []},
        {"count": 2, "tokens": [1]},
        {"count": 1, "tokens": [True]},
        {"count": 1, "tokens": [-1]},
        {"count": 0, "tokens": []},
    ],
)
def test_native_token_observation_rejects_invalid_counts(response) -> None:
    with pytest.raises(ValueError):
        BodyTokens.from_response("text", response)


def budget_fixture():
    hits = [
        Hit(
            id=str(i),
            document_id="doc",
            start=i * 10,
            end=i * 10 + len(text),
            text=text,
        )
        for i, text in enumerate(("first", "second", "third"))
    ]
    run = Run(
        dataset_sha256="f" * 64,
        top_k=5,
        settings={
            "model": "test",
            "backend": "test",
            "strategy": "page",
            "mode": "hybrid",
            "level": "child",
            "dim": 1,
            "rerank": False,
            "implementation_sha256": "test",
        },
        results=[
            QueryRun(query_id="q", hits=hits, elapsed_ms=10, fetched=3, reranked=False)
        ],
    )
    counts = {
        h.id: BodyTokens(sha256(h.text.encode()).hexdigest(), n)
        for h, n in zip(hits, [3, 4, 1], strict=True)
    }
    return run, counts


def test_body_budget_keeps_whole_ranked_prefix_without_skipping_or_rewriting() -> None:
    run, counts = budget_fixture()
    original = run.model_dump()
    limited = body_budget(
        run, counts, limit=5, tokenizer_model="native", tokenizer_revision="unknown"
    )
    assert limited.results[0].hits == run.results[0].hits[:1]
    assert limited.results[0].elapsed_ms is None
    assert limited.results[0].fetched == 3
    assert limited.settings["tokenizer_revision"] == "unknown"
    assert (
        limited.settings["budget_scope"]
        == "sum of separately tokenized returned bodies; whole ranked prefix"
    )
    assert (
        body_budget(
            run, counts, limit=7, tokenizer_model="native", tokenizer_revision="unknown"
        )
        .results[0]
        .hits
        == run.results[0].hits[:2]
    )
    assert (
        not body_budget(
            run, counts, limit=2, tokenizer_model="native", tokenizer_revision="unknown"
        )
        .results[0]
        .hits
    )
    assert run.model_dump() == original


def test_body_budget_rejects_token_observations_for_other_text() -> None:
    run, counts = budget_fixture()
    counts["0"] = BodyTokens("0" * 64, 3)
    with pytest.raises(ValueError, match="原文身份"):
        body_budget(
            run, counts, limit=5, tokenizer_model="native", tokenizer_revision="unknown"
        )


@pytest.mark.parametrize("limit", [0, -1, True])
def test_body_budget_requires_a_real_positive_integer(limit) -> None:
    run, counts = budget_fixture()
    with pytest.raises(ValueError, match="正整数"):
        body_budget(
            run,
            counts,
            limit=limit,
            tokenizer_model="native",
            tokenizer_revision="unknown",
        )


@pytest.mark.parametrize(
    "fault", ["empty_samples", "empty_pages", "bytes", "text", "page_order"]
)
def test_pdf_source_verification_cannot_pass_without_real_matching_pages(
    tmp_path, monkeypatch, fault
) -> None:
    from types import SimpleNamespace

    from evals.pdf import verify

    content = b"%PDF-test-fixture"
    (tmp_path / "doc.pdf").write_bytes(content)
    snapshot = {
        "version": 1,
        "extractor": "pypdf 6.10.0 plain",
        "samples": [
            {
                "source": {"id": "doc"},
                "pdf_bytes": len(content),
                "pdf_sha256": sha256(content).hexdigest(),
                "total_pages": 2,
                "pages": [
                    {"page_number": 1, "text": "page one"},
                    {"page_number": 2, "text": "page two"},
                ],
            }
        ],
    }
    pages = [
        SimpleNamespace(extract_text=lambda: "page one"),
        SimpleNamespace(extract_text=lambda: "page two"),
    ]
    monkeypatch.setattr(
        verify,
        "import_module",
        lambda name: SimpleNamespace(
            PdfReader=lambda path: SimpleNamespace(pages=pages)
        ),
    )
    monkeypatch.setattr(verify, "version", lambda name: "6.10.0")
    if fault == "empty_samples":
        snapshot["samples"] = []
    elif fault == "empty_pages":
        snapshot["samples"][0]["pages"] = []
    elif fault == "bytes":
        (tmp_path / "doc.pdf").write_bytes(b"corrupt")
    elif fault == "text":
        snapshot["samples"][0]["pages"][1]["text"] = "wrong"
    else:
        snapshot["samples"][0]["pages"].reverse()
    (tmp_path / "pages.json").write_text(json.dumps(snapshot))
    with pytest.raises(ValueError):
        verify.verify(tmp_path, data=tmp_path)
