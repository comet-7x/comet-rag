from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from evals.retrieval.data import (
    DATA,
    Corpus,
    Dataset,
    Judgments,
    generate,
    locate_evidence,
    read_json,
    validate_sources,
)
from evals.retrieval.metrics import Hit, QueryRun, Run, score


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    return generate(tmp_path_factory.mktemp("retrieval-eval") / "corpus")


def full_hit(dataset: Dataset, did: str, hid: str | None = None) -> Hit:
    text = dataset.documents[did].markdown
    return Hit(id=hid or did, document_id=did, start=0, end=len(text), text=text)


def run_with(dataset: Dataset, query: str, hits: list[Hit]) -> Run:
    return Run(
        dataset_sha256=dataset.fingerprint,
        top_k=5,
        settings={
            "model": "test",
            "backend": "test",
            "strategy": "test",
            "mode": "dense",
            "level": "child",
            "dim": 1,
            "rerank": False,
            "implementation_sha256": "test",
        },
        results=[
            QueryRun(query_id=q.id, hits=hits if q.id == query else [])
            for q in dataset.judgments.queries
        ],
    )


def row(dataset: Dataset, run: Run, qid: str) -> dict[str, object]:
    report = score(dataset, run)
    rows = report["per_query"]
    assert isinstance(rows, list)
    return next(r for r in rows if r["query_id"] == qid)


def test_ranking_uses_raw_ranks_and_duplicate_docs_get_no_extra_credit(dataset) -> None:
    # 相关度 star=2、billing=1；重复 star 占第三位，不能把 billing 提前到第三位。
    hits = [
        full_hit(dataset, "access"),
        full_hit(dataset, "star", "s1"),
        full_hit(dataset, "star", "s2"),
        full_hit(dataset, "billing"),
    ]
    result = row(
        dataset, run_with(dataset, "retention_compare", hits), "retention_compare"
    )
    assert result["recall@1"] == 0
    assert result["recall@3"] == 0.5
    assert result["recall@5"] == 1
    assert result["rr@5"] == 0.5
    # 手算 (3/log2(3)+1/log2(5)) / (3+1/log2(3))，独立于实现。
    assert result["ndcg@5"] == pytest.approx(0.639909328045346)
    assert result["evidence@3"] == 0.5
    assert result["evidence@5"] == 1


def test_doc_hit_is_not_evidence_and_fragments_cannot_be_joined(dataset) -> None:
    start, end = dataset.spans["backup"]
    text = dataset.documents["star"].markdown
    midpoint = (start + end) // 2
    fragments = [
        Hit(
            id="a",
            document_id="star",
            start=start,
            end=midpoint,
            text=text[start:midpoint],
        ),
        Hit(
            id="b", document_id="star", start=midpoint, end=end, text=text[midpoint:end]
        ),
    ]
    result = row(dataset, run_with(dataset, "backup_time", fragments), "backup_time")
    assert result["recall@5"] == 1
    assert result["evidence@5"] == 0
    exact = Hit(id="c", document_id="star", start=start, end=end, text=text[start:end])
    assert (
        row(dataset, run_with(dataset, "backup_time", [exact]), "backup_time")[
            "evidence@1"
        ]
        == 1
    )


def test_no_answer_is_excluded_from_positive_macro_and_reported_separately(
    dataset,
) -> None:
    run = run_with(dataset, "no_answer_price", [full_hit(dataset, "star")])
    run.results[0].hits = [full_hit(dataset, "star")]
    report = score(dataset, run)
    result = row(dataset, run, "no_answer_price")
    assert result["recall@5"] is None
    assert result["ndcg@5"] is None
    assert report["positive_queries"] == 16
    assert report["no_answer_queries"] == 2
    assert report["no_answer_return_rate@5"] == 0.5
    assert isinstance(report["macro"], dict)
    assert report["macro"]["recall@5"] == 1 / 16


@pytest.mark.parametrize("did", ["star_old", "star_lab", "unknown"])
def test_forbidden_document_identity_fails_instead_of_being_filtered(
    dataset, did
) -> None:
    hit = (
        full_hit(dataset, did)
        if did in dataset.documents
        else full_hit(dataset, "star").model_copy(update={"document_id": did})
    )
    with pytest.raises(ValueError, match="未知、跨 KB 或 inactive"):
        score(dataset, run_with(dataset, "backup_time", [hit]))


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "extra",
        "duplicate",
        "fingerprint",
        "degraded",
        "error",
        "duplicate_hit",
        "too_many",
        "text",
        "offset",
    ],
)
def test_malformed_runs_fail_closed(dataset, defect) -> None:
    run = run_with(dataset, "backup_time", [full_hit(dataset, "star")])
    if defect == "missing":
        run.results.pop()
    elif defect == "extra":
        run.results.append(QueryRun(query_id="extra", hits=[]))
    elif defect == "duplicate":
        run.results[-1] = run.results[0]
    elif defect == "fingerprint":
        run.dataset_sha256 = "0" * 64
    elif defect == "degraded":
        run.results[0].degraded = True
    elif defect == "error":
        run.results[0].error = "timeout"
    elif defect == "duplicate_hit":
        run.results[0].hits *= 2
    elif defect == "too_many":
        run.results[0].hits = [full_hit(dataset, "star", f"h{i}") for i in range(6)]
    elif defect == "text":
        run.results[0].hits[0].text += "伪造"
    else:
        run.results[0].hits[0].end += 1
    with pytest.raises(ValueError):
        score(dataset, run)


@pytest.mark.parametrize("value", [True, "1", 1.0, float("nan"), float("inf"), -1, 3])
def test_labels_reject_bool_strings_fractions_nonfinite_and_invalid_grades(
    value,
) -> None:
    from evals.retrieval.data import Query

    with pytest.raises(ValidationError):
        Query(
            id="q",
            kb_id="kb",
            text="question",
            category="fact",
            relevance={"d": value},
            evidence=["e"],
        )


def test_json_duplicate_keys_are_rejected(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"version":1,"version":1,"documents":[]}')
    with pytest.raises(ValueError, match="重复 JSON 键"):
        read_json(path, Corpus)


def test_latency_is_observed_nearest_rank_and_optional(dataset) -> None:
    run = run_with(dataset, "backup_time", [])
    for result, value in zip(run.results[:4], [10.0, 40.0, 20.0, 30.0], strict=True):
        result.elapsed_ms = value
    assert score(dataset, run)["latency_ms"] == {"samples": 4, "p50": 20.0, "p95": 40.0}
    with pytest.raises(ValidationError):
        QueryRun(query_id="q", hits=[], elapsed_ms=float("inf"))


@pytest.mark.parametrize(
    "key,value",
    [("model", ""), ("backend", False), ("dim", True), ("dim", 0), ("rerank", 1)],
)
def test_run_requires_explicit_model_and_configuration_identity(
    dataset, key, value
) -> None:
    raw = run_with(dataset, "backup_time", []).model_dump()
    raw["settings"][key] = value
    with pytest.raises(ValidationError):
        Run.model_validate(raw)


@pytest.mark.parametrize(
    "defect",
    [
        "missing_quote",
        "duplicate_quote",
        "inactive",
        "cross_kb",
        "no_answer",
        "unknown_evidence",
        "uncovered_doc",
        "revision",
        "active",
        "table",
    ],
)
def test_corpus_and_judgment_contracts(dataset, tmp_path, defect) -> None:
    corpus = deepcopy(dataset.corpus.model_dump())
    labels = deepcopy(dataset.judgments.model_dump())
    if defect == "missing_quote":
        labels["evidence"][0]["quote"] = "文档没有这个答案"
    elif defect == "duplicate_quote":
        corpus["documents"][1]["sections"][0]["paragraphs"] *= 2
    elif defect == "inactive":
        labels["evidence"][0]["document_id"] = "star_old"
    elif defect == "cross_kb":
        labels["queries"][0]["kb_id"] = "lab"
    elif defect == "no_answer":
        labels["queries"][0]["category"] = "no_answer"
    elif defect == "unknown_evidence":
        labels["queries"][0]["evidence"] = ["missing"]
    elif defect == "uncovered_doc":
        labels["queries"][0]["relevance"]["billing"] = 1
    elif defect == "revision":
        corpus["documents"][1]["revision"] = corpus["documents"][0]["revision"]
    elif defect == "active":
        corpus["documents"][0]["active"] = True
    else:
        corpus["documents"][4]["sections"][0]["table"][1].pop()
    with pytest.raises(ValueError):
        parsed = Corpus.model_validate(corpus)
        validate_sources(parsed)
        documents = dict(dataset.documents)
        if defect == "duplicate_quote":
            from comet_rag.ports.document import NormalizedDocument

            documents["star"] = NormalizedDocument(documents["star"].markdown * 2)
        locate_evidence(parsed, Judgments.model_validate(labels), documents)


def test_checked_in_data_has_no_embedded_generated_files() -> None:
    assert not list(DATA.glob("*.docx"))
    assert not list(DATA.glob("*.run.json"))
