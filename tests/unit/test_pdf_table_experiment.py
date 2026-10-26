from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from comet_rag.services.retrieval import ReturnLevel, SearchMode
from evals.pdf.artifacts import load_frozen
from evals.pdf.inspect import table_integrity
from evals.pdf.table_data import CASES, ROW_SEPARATORS, joint_report, load_cases
from evals.retrieval.baseline import collect
from evals.retrieval.metrics import Hit, QueryRun, Run


@pytest.fixture(scope="module")
def dataset():
    return load_cases()


@pytest.fixture(scope="module")
def native():
    return load_frozen()


def run_for(dataset, *, combined: bool) -> Run:
    results = []
    for query in dataset.judgments.queries:
        spans = [dataset.spans[eid] for eid in query.evidence]
        chosen = [(spans[0][0], spans[1][1])] if combined else spans
        text = dataset.documents["nist144"].markdown
        results.append(
            QueryRun(
                query_id=query.id,
                hits=[
                    Hit(
                        id=f"{query.id}-{i}",
                        document_id="nist144",
                        start=a,
                        end=b,
                        text=text[a:b],
                    )
                    for i, (a, b) in enumerate(chosen)
                ],
                reranked=False,
                fetched=len(chosen),
            )
        )
    return Run(
        dataset_sha256=dataset.fingerprint,
        top_k=5,
        settings={
            "model": "fixture",
            "backend": "fixture",
            "dim": 1,
            "rerank": False,
            "strategy": "hierarchy",
            "mode": "hybrid",
            "level": "parent" if combined else "child",
            "implementation_sha256": "fixture",
        },
        results=results,
    )


def test_native_table_subset_has_own_identity_and_exact_page_domain(dataset) -> None:
    assert (
        dataset.fingerprint
        == "278a3e92fc64ff5308e0bb98d23769d260b65d4abcc5e1059867a9058b8a9f2d"
    )
    assert [q.id for q in dataset.judgments.queries] == ["q017", "q018"]
    assert len(dataset.judgments.evidence) == 4
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
    assert dataset.spans == {
        "q017-header": (24807, 24854),
        "q017-row": (25996, 26065),
        "q018-header": (24807, 24854),
        "q018-row": (26164, 26285),
    }


def test_fragments_in_two_hits_do_not_become_one_complete_joint_unit(dataset) -> None:
    report = joint_report(dataset, run_for(dataset, combined=False))
    macro, units, status = (
        report["macro"],
        report["joint_units"],
        report["quality_acceptance"],
    )
    assert (
        isinstance(macro, dict) and isinstance(units, list) and isinstance(status, str)
    )
    assert macro["evidence@5"] == 1.0
    assert [r["one_hit_joint@5"] for r in units] == [False, False]
    assert [r["minimum_contiguous_characters"] for r in units] == [
        1258,
        1478,
    ]
    assert "author-provisional" in status


def test_one_returned_parent_can_contain_both_literal_parts(dataset) -> None:
    report = joint_report(dataset, run_for(dataset, combined=True))
    units = report["joint_units"]
    assert isinstance(units, list)
    assert [r["one_hit_joint@1"] for r in units] == [True, True]


def test_joint_metric_cannot_silently_drop_the_required_header(dataset) -> None:
    edited = replace(dataset, judgments=dataset.judgments.model_copy(deep=True))
    edited.judgments.queries[0].evidence.pop(0)
    with pytest.raises(ValueError, match="表头和数据行"):
        joint_report(edited, run_for(dataset, combined=False))


@pytest.mark.parametrize(
    "fault",
    [
        "canonical",
        "quote",
        "span",
        "role",
        "page",
        "document",
        "bool_version",
        "approved",
        "partial_row",
    ],
)
def test_table_case_corruption_fails_before_model_collection(
    tmp_path: Path, monkeypatch, native, fault: str
) -> None:
    import evals.pdf.table_data as module

    # 共享只读的已核验原生值，故障仍完整走标签、区间及HTML行校验。
    monkeypatch.setattr(module, "load_frozen", lambda *args, **kwargs: native)
    raw = json.loads(CASES.read_text())
    case = raw["cases"][0]
    if fault == "canonical":
        raw["full_canonical_sha256"] = "0" * 64
    elif fault == "quote":
        # 合法但属于另一行的quote，不能让下游唯一搜索替代已声明区间的校验。
        case["row"]["quote"] = raw["cases"][1]["row"]["quote"]
    elif fault == "span":
        case["row"]["start_char"] += 1
    elif fault == "role":
        case["header"]["role"] = "table_row"
    elif fault == "page":
        case["row"]["page_number"] = 51
    elif fault == "document":
        case["row"]["document_id"] = "nist145"
    elif fault == "bool_version":
        raw["version"] = True
    elif fault == "partial_row":
        case["row"]["start_char"] += 8
        case["row"]["quote"] = case["row"]["quote"][8:]
    else:
        raw["label_status"] = "independent_approved"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_cases(cases=path)


def test_row_separator_profile_keeps_rows_without_claiming_whole_tables(native) -> None:
    tables = table_integrity(native[0]["nist144"], separators=ROW_SEPARATORS)
    assert [(t["physical_page"], t["rows"]) for t in tables] == [
        (10, 18),
        (45, 4),
        (46, 7),
        (52, 16),
        (61, 10),
        (80, 9),
    ]
    for table in tables:
        page, child, parent = (
            table["page_child"],
            table["hierarchy_child"],
            table["parent"],
        )
        assert (
            isinstance(page, dict)
            and isinstance(child, dict)
            and isinstance(parent, dict)
        )
        assert page["whole_rows"] == table["rows"]
        assert child["whole_rows"] == table["rows"]
        assert parent["whole_rows"] == table["rows"]
        assert not page["whole_table"] and not parent["whole_table"]


async def test_evaluation_collector_records_and_uses_profile_without_changing_defaults(
    dataset,
) -> None:
    default = await collect(
        dataset,
        strategy="page",
        mode=SearchMode.HYBRID,
        child_size=800,
        child_overlap=80,
        parent_size=1600,
    )
    rows = await collect(
        dataset,
        strategy="page",
        mode=SearchMode.HYBRID,
        child_size=800,
        child_overlap=80,
        parent_size=1600,
        separators=ROW_SEPARATORS,
    )
    assert "separator_profile" not in default.settings
    profile = rows.settings["separator_profile"]
    assert isinstance(profile, str)
    assert json.loads(profile) == list(ROW_SEPARATORS)
    assert (
        default.settings["indexed_children"],
        rows.settings["indexed_children"],
    ) == (72, 70)
    assert (
        default.settings["indexed_text_chars"],
        rows.settings["indexed_text_chars"],
    ) == (35955, 35888)


async def test_collector_applies_profile_to_parents_as_well_as_children(
    dataset,
) -> None:
    values = []
    for separators in (None, ROW_SEPARATORS):
        run = await collect(
            dataset,
            strategy="hierarchy",
            level=ReturnLevel.PARENT,
            mode=SearchMode.HYBRID,
            child_size=800,
            child_overlap=80,
            parent_size=1600,
            separators=separators,
        )
        values.append(
            (
                run.settings["indexed_children"],
                run.settings["stored_parents"],
                run.settings["parent_text_chars"],
            )
        )
    assert values == [(72, 34, 34873), (71, 32, 34874)]


def model_config():
    from comet_rag.config.schemas import (
        APPConfig,
        EmbeddingModelConfig,
        InfrastructureConfig,
        ModelConfig,
        ServerConfig,
    )

    return APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=8000),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://embedding.example.test/v1",
                model_name="fixture",
                dim=256,
            ),
            reranker=ModelConfig(
                base_url="http://reranker.example.test/v1", model_name="fixture"
            ),
        ),
    )


async def test_existing_experiment_directory_stops_before_model_construction(
    tmp_path: Path, monkeypatch, dataset, native
) -> None:
    import evals.pdf.table_experiment as module

    monkeypatch.setattr(module, "load_cases", lambda **kwargs: dataset)
    monkeypatch.setattr(module, "load_frozen", lambda *args: native)

    def forbidden(*args):
        raise AssertionError("Must not create model")

    monkeypatch.setattr(module, "build_embedding_model", forbidden)
    out = tmp_path / "existing"
    out.mkdir()
    with pytest.raises(FileExistsError):
        await module.evaluate(model_config(), out)


async def test_reranker_initialization_failure_closes_the_created_embedding(
    tmp_path: Path, monkeypatch, dataset, native
) -> None:
    import evals.pdf.table_experiment as module

    closed = []

    class AsyncModel:
        async def close(self):
            closed.append(True)

    def failed(*args):
        raise RuntimeError("injected reranker initialization failure")

    monkeypatch.setattr(module, "load_cases", lambda **kwargs: dataset)
    monkeypatch.setattr(module, "load_frozen", lambda *args: native)
    monkeypatch.setattr(module, "build_embedding_model", lambda *args: AsyncModel())
    monkeypatch.setattr(module, "build_reranker", failed)
    out = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="injected"):
        await module.evaluate(model_config(), out)
    assert closed == [True]
    assert not (out / "summary.json").exists()


def test_row_profile_does_not_relax_budget_for_an_oversized_row() -> None:
    from comet_rag.ports import DocumentBlock, NormalizedDocument

    text = "<table><tr><td>" + "A" * 900 + "</td></tr><tr><td>B</td></tr></table>"
    document = NormalizedDocument(
        text, blocks=(DocumentBlock("page", 0, 0, len(text), page_number=1),)
    )
    report = table_integrity(document, separators=ROW_SEPARATORS)[0]
    assert report["rows_over_child_budget"] == 1
    assert report["page_child"] == {"whole_table": False, "whole_rows": 1}
    assert report["parent"] == {"whole_table": True, "whole_rows": 2}


def test_selected_page_scope_is_part_of_each_derived_revision(
    tmp_path: Path, monkeypatch, dataset, native
) -> None:
    import evals.pdf.table_data as module
    from evals.pdf.data import DATA

    monkeypatch.setattr(module, "load_frozen", lambda *args, **kwargs: native)
    snapshot = json.loads((DATA / "pages.json").read_text())
    snapshot["samples"][0]["pages"].insert(
        0,
        {
            "page_number": 4,
            "text": "selection metadata fixture; native body still comes from locked full corpus",
        },
    )
    (tmp_path / "pages.json").write_text(json.dumps(snapshot))
    changed = load_cases(data=tmp_path)
    assert changed.corpus.documents[0].revision != dataset.corpus.documents[0].revision
    assert changed.corpus.documents[1].revision == dataset.corpus.documents[1].revision
