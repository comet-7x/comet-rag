from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from comet_rag.ports.content import RankedDocument, RerankDocument
from comet_rag.ports.document import NormalizedDocument
from evals.retrieval.data import (
    Corpus,
    Dataset,
    Evidence,
    Judgments,
    Query,
    Section,
    Source,
)
from evals.retrieval.metrics import Hit, QueryRun, Run, score
from evals.retrieval.title_context import title_view
from evals.retrieval.title_replay import (
    AsyncRecordingReranker,
    CandidatePool,
    pool_fingerprint,
    replay,
)


def test_title_views_are_explicit_bounded_and_identity_specific() -> None:
    md = {"block_kind": "section", "heading_path": ["星桥", "恢复"]}
    original = deepcopy(md)
    assert title_view("先核对。", md, "plain") == "先核对。"
    assert (
        title_view("先核对。", md, "headings")
        == "标题路径（仅定位信息）：星桥 / 恢复\n\n正文：\n先核对。"
    )
    assert (
        title_view("先核对。", md, "ancestors")
        == "标题路径（仅定位信息）：星桥\n\n正文：\n先核对。"
    )
    other = {"block_kind": "section", "heading_path": ["港湾", "恢复"]}
    assert "港湾" in title_view("先核对。", other, "headings")
    assert "星桥" not in title_view("先核对。", other, "headings")
    assert md == original


@pytest.mark.parametrize(
    "path",
    [
        None,
        "星桥",
        [],
        [""],
        ["  "],
        ["星桥", 3],
        ["甲"] * 7,
        ["甲" * 81],
        ["甲" * 80] * 4,
    ],
)
def test_missing_or_invalid_paths_fall_back_without_partial_truncation(path) -> None:
    for policy in ("headings", "ancestors"):
        assert (
            title_view("原文", {"block_kind": "section", "heading_path": path}, policy)
            == "原文"
        )


@pytest.mark.parametrize("kind", ["page", "unknown", None])
def test_page_or_unknown_blocks_never_borrow_section_titles(kind) -> None:
    assert (
        title_view(
            "第2页原文",
            {"block_kind": kind, "heading_path": ["第1页秘密", "标题"]},
            "headings",
        )
        == "第2页原文"
    )


def test_root_has_no_ancestor_and_bounds_are_inclusive() -> None:
    assert (
        title_view(
            "原文", {"block_kind": "section", "heading_path": ["根"]}, "ancestors"
        )
        == "原文"
    )
    path = ["甲" * 80, "乙" * 80, "丙" * 80, "丁" * 16]
    assert title_view(
        "原文", {"block_kind": "section", "heading_path": path}, "headings"
    ).endswith("正文：\n原文")
    with pytest.raises(ValueError, match="未知"):
        title_view("原文", {}, "typo")  # type: ignore[arg-type] — 验证未校验调用方


@pytest.fixture(scope="module")
def fixture():
    # 视图/评分边界只需手写原文；DOCX 生成与真实分块另由 benchmark 验证。
    prefix = "这是没有具体时间的标题说明。\n\n"
    quote = "星桥每天 03:10 备份，保留 14 天。"
    assert len(prefix) == 16
    text = prefix + quote
    source = Source(
        id="star",
        kb_id="ops",
        source_id="star",
        revision="new",
        active=True,
        format="md",
        title="手册",
        sections=[Section(heading="备份", paragraphs=[quote])],
    )
    old = source.model_copy(
        update={"id": "star_old", "revision": "old", "active": False}
    )
    query = Query(
        id="backup_time",
        kb_id="ops",
        text="星桥何时备份？",
        category="fact",
        relevance={"star": 2},
        evidence=["backup"],
    )
    dataset = Dataset(
        corpus=Corpus(version=1, documents=[source, old]),
        judgments=Judgments(
            version=1,
            queries=[query],
            evidence=[Evidence(id="backup", document_id="star", quote=quote)],
        ),
        documents={
            "star": NormalizedDocument(text),
            "star_old": NormalizedDocument("旧版"),
        },
        spans={"backup": (16, len(text))},
        fingerprint="f" * 64,
    )
    # 前缀故意携带完整答案；正文仍是标题孤块，不能因此获得证据分。
    document = RerankDocument(
        id="raw",
        content=text[:16],
        metadata={
            "kb_id": source.kb_id,
            "source_id": source.source_id,
            "document_revision": source.revision,
            "chunk_start": 0,
            "chunk_end": 16,
            "block_kind": "section",
            "heading_path": [quote, "恢复"],
        },
    )
    pool = CandidatePool(query.id, query.text, (document,))
    run = Run(
        dataset_sha256=dataset.fingerprint,
        top_k=5,
        settings={
            "model": "test",
            "backend": "test",
            "strategy": "hierarchy",
            "mode": "dense",
            "level": "child",
            "dim": 1,
            "rerank": True,
            "implementation_sha256": "test",
        },
        results=[
            QueryRun(
                query_id=query.id,
                hits=[
                    Hit(id="raw", document_id="star", start=0, end=16, text=text[:16])
                ],
                fetched=1,
                reranked=True,
            )
        ],
    )
    return dataset, pool, run


class ObservedReranker:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def rank(self, query, documents, /, *, top_k=None, **kwargs):
        self.calls.append((query, deepcopy(documents)))
        # 供应商回传内容不可信，结果必须按索引指回原始候选。
        return [
            RankedDocument(
                index=i, score=1.0, document=RerankDocument(content="不可信替换")
            )
            for i in reversed(range(len(documents)))
        ]

    def bind_gate(self, gate):
        pass

    async def close(self):
        self.closed = True


async def test_replay_cannot_credit_prefix_as_evidence_or_return_model_view(
    fixture,
) -> None:
    dataset, pool, capture = fixture
    before = pool_fingerprint([pool])
    reranker = ObservedReranker()
    runs = await replay(dataset, capture, [pool], reranker)
    assert len(reranker.calls) == 3
    assert "标题路径" in reranker.calls[1][1][0].content
    for run in runs.values():
        assert run.results[0].hits == capture.results[0].hits
        macro = score(dataset, run)["macro"]
        assert isinstance(macro, dict)
        assert macro["evidence@5"] == 0
        assert run.settings["pool_sha256"] == before
        assert (
            run.settings["timing_scope"]
            == "reranker replay only; not retrieval latency"
        )
    assert pool_fingerprint([pool]) == before
    assert not reranker.closed


async def test_recording_snapshots_inputs_and_keeps_delegate_caller_owned(
    fixture,
) -> None:
    dataset, pool, capture = fixture
    reranker = ObservedReranker()
    recorder = AsyncRecordingReranker(reranker)
    document = deepcopy(pool.documents[0])
    await recorder.rank(pool.query, [document])
    document.metadata["heading_path"].append("调用后污染")
    pools = recorder.pools(dataset, capture)
    assert (
        pools[0].documents[0].metadata["heading_path"]
        == pool.documents[0].metadata["heading_path"]
    )
    await recorder.close()
    assert not reranker.closed
    with pytest.raises(ValueError, match="身份"):
        await recorder.rank(pool.query, ["裸字符串"])


@pytest.mark.parametrize(
    "fault", ["missing", "query", "duplicate", "span", "old", "kb"]
)
async def test_replay_rejects_corrupt_pool_before_model_call(fixture, fault) -> None:
    dataset, pool, capture = fixture
    document = deepcopy(pool.documents[0])
    pools = [replace(pool, documents=(document,))]
    if fault == "missing":
        pools = []
    elif fault == "query":
        pools = [replace(pool, query="另一个问题")]
    elif fault == "duplicate":
        pools = [replace(pool, documents=(document, document))]
    elif fault == "span":
        document.metadata["chunk_end"] = 17
    elif fault == "old":
        source = next(s for s in dataset.corpus.documents if s.id == "star_old")
        document.metadata["document_revision"] = source.revision
    else:
        document.metadata["kb_id"] = "other"
    reranker = ObservedReranker()
    with pytest.raises((ValueError, KeyError)):
        await replay(dataset, capture, pools, reranker)
    assert not reranker.calls


async def test_model_failure_or_invalid_permutation_cannot_be_scored_as_healthy(
    fixture,
) -> None:
    dataset, pool, capture = fixture

    class Invalid(ObservedReranker):
        async def rank(self, *args, **kwargs):
            return []

    with pytest.raises(ValueError):
        await replay(dataset, capture, [pool], Invalid())

    class Broken(ObservedReranker):
        async def rank(self, *args, **kwargs):
            raise RuntimeError("injected model failure")

    with pytest.raises(RuntimeError, match="injected"):
        await replay(dataset, capture, [pool], Broken())


async def test_indices_map_to_raw_candidates_and_views_cannot_mutate_pool(
    fixture,
) -> None:
    dataset, pool, capture = fixture
    first = pool.documents[0]
    start, end = dataset.spans["backup"]
    second = RerankDocument(
        id="answer",
        content=dataset.documents["star"].markdown[start:end],
        metadata={
            **first.metadata,
            "chunk_start": start,
            "chunk_end": end,
            "heading_path": ["星桥", "正文"],
        },
    )
    pool = replace(pool, documents=(first, second))
    before = pool_fingerprint([pool])

    class Mutating(ObservedReranker):
        async def rank(self, query, documents, /, **kwargs):
            documents[0].metadata["heading_path"].append("供应商污染")
            return await super().rank(query, documents, **kwargs)

    runs = await replay(dataset, capture, [pool], Mutating())
    for run in runs.values():
        assert [hit.id for hit in run.results[0].hits] == ["answer", "raw"]
        assert run.results[0].hits[0].text == second.content
        assert run.results[0].hits[0].start == start
        assert run.results[0].hits[0].end == end
    assert pool_fingerprint([pool]) == before


@pytest.mark.parametrize("fault", ["missing", "extra", "text", "count"])
async def test_recorder_rejects_unpaired_calls(fixture, fault) -> None:
    dataset, pool, capture = fixture
    recorder = AsyncRecordingReranker(ObservedReranker())
    if fault != "missing":
        await recorder.rank(
            "wrong" if fault == "text" else pool.query,
            pool.documents * (2 if fault == "count" else 1),
        )
    if fault == "extra":
        await recorder.rank(pool.query, pool.documents)
    with pytest.raises(ValueError, match="输入|未配对"):
        recorder.pools(dataset, capture)


async def test_replay_rotates_policy_order_without_reordering_queries(fixture) -> None:
    dataset, pool, capture = fixture
    first = dataset.judgments.queries[0]
    second = first.model_copy(update={"id": "second"})
    dataset = replace(
        dataset,
        judgments=dataset.judgments.model_copy(update={"queries": [first, second]}),
    )
    capture = capture.model_copy(deep=True)
    capture.results.append(capture.results[0].model_copy(update={"query_id": "second"}))
    reranker = ObservedReranker()
    runs = await replay(
        dataset, capture, [pool, replace(pool, query_id="second")], reranker
    )
    views = [documents[0].content for _, documents in reranker.calls]
    assert views[0] == views[5] == pool.documents[0].content
    assert views[1] == views[3] and views[2] == views[4]
    for run in runs.values():
        assert [r.query_id for r in run.results] == [first.id, "second"]


async def test_empty_candidate_pool_never_fabricates_rerank_completion(fixture) -> None:
    dataset, pool, capture = fixture
    capture = capture.model_copy(deep=True)
    capture.results[0] = QueryRun(
        query_id=pool.query_id, hits=[], fetched=0, reranked=False
    )
    pool = replace(pool, documents=())
    reranker = ObservedReranker()
    assert AsyncRecordingReranker(reranker).pools(dataset, capture) == (pool,)
    runs = await replay(dataset, capture, [pool], reranker)
    assert not reranker.calls
    assert all(
        not run.results[0].reranked and not run.results[0].hits for run in runs.values()
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_forwards_explicit_title_context_without_connecting_models(
    monkeypatch, tmp_path, enabled
) -> None:
    import sys

    from evals.retrieval import __main__, real

    observed = {}

    async def evaluate(config, output, **options):
        observed.update(config=config, output=output, **options)

    monkeypatch.setattr(real, "evaluate_models", evaluate)
    args = [
        "evals.retrieval",
        "real",
        "--config",
        str(tmp_path / "models.yaml"),
        "--out",
        str(tmp_path / "result"),
        "--model-revision",
        "unknown",
    ]
    if enabled:
        args.append("--title-context")
    monkeypatch.setattr(sys, "argv", args)
    __main__.main()
    assert observed["title_context"] is enabled
    assert observed["config"] == tmp_path / "models.yaml"
    assert observed["model_revision"] == "unknown"
