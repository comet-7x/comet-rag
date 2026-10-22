from __future__ import annotations

from collections.abc import Sequence

import pytest

from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.models.reranker.base import AsyncBaseReranker
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import Filter, NormalizedDocument, SearchHit
from comet_rag.ports.document_store import RevisionKey, StoredParent
from comet_rag.services.indexing import IndexPlanner, RevisionStager
from comet_rag.services.retrieval import (
    RetrievalService,
    ReturnLevel,
    SearchMode,
    SearchQuery,
)
from comet_rag.services.revision_recall import RevisionRecall, RevisionVisibilityError


class Embedding(AsyncBaseEmbeddingModel):
    def __init__(self) -> None:
        self.calls = 0

    def _vector(self, data, **kwargs) -> list[float]:
        self.calls += 1
        return [1.0, 0.0]

    async def _embed(self, data, **kwargs) -> list[float]:
        return self._vector(data)

    async def close(self) -> None:
        return None


class Candidates(AsyncInMemoryVectorStore):
    def __init__(
        self, dense: list[SearchHit], keyword: list[SearchHit] | None = None
    ) -> None:
        super().__init__()
        self.dense = dense
        self.keyword = keyword if keyword is not None else dense
        self.limits: dict[str, list[int]] = {"dense": [], "keyword": []}
        self.dense_failure = False

    async def search(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        self.limits["dense"].append(top_k)
        if self.dense_failure:
            raise ConnectionError("vector unavailable")
        return self.dense[:top_k]

    async def search_keywords(
        self,
        kb_id: str,
        query: str,
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        self.limits["keyword"].append(top_k)
        return self.keyword[:top_k]


class Documents(InMemoryDocumentStore):
    def __init__(self) -> None:
        super().__init__()
        self.active: dict[str, str | None] = {"source": "new", "legacy": None}
        self.lookups: list[list[str]] = []
        self.parent_lookups: list[list[str]] = []
        self.parents: dict[str, StoredParent] = {}
        self.failure = False
        self.parent_failure = False
        self.incomplete = False

    async def get_active_revisions(
        self,
        kb_id: str,
        source_ids: Sequence[str],
    ) -> dict[str, str | None]:
        self.lookups.append(list(source_ids))
        if self.failure:
            raise ConnectionError("postgresql://secret-password@private-host")
        if self.incomplete:
            return {}
        return {source: self.active.get(source) for source in source_ids}

    async def get_parents(
        self,
        kb_id: str,
        parent_ids: Sequence[str],
    ) -> dict[str, StoredParent]:
        self.parent_lookups.append(list(parent_ids))
        if self.parent_failure:
            raise ConnectionError("postgresql://secret-password@private-host")
        return {pid: self.parents[pid] for pid in parent_ids if pid in self.parents}


def hit(
    rid: str,
    revision: str | None = "new",
    *,
    source: str = "source",
    parent: str | None = None,
) -> SearchHit:
    metadata: dict[str, object] = {"kb_id": "kb", "source_id": source}
    if revision is not None:
        metadata["document_revision"] = revision
    if parent is not None:
        metadata["parent_id"] = parent
    return SearchHit(rid, f"child {rid}", 1.0, metadata)


def service(
    vectors: Candidates, documents: Documents | None, *, reranker=None
) -> tuple[RetrievalService, Embedding]:
    model = Embedding()
    return RetrievalService(
        embedding_model=model,
        vector_store=vectors,
        keyword_search=vectors,
        document_store=documents,
        reranker=reranker,
    ), model


@pytest.mark.parametrize("cap", [0, -1, 2049, True, 1.5, "64"])
def test_invalid_candidate_budgets_are_rejected(cap) -> None:
    with pytest.raises(ValueError):
        RevisionRecall(Documents(), max_candidates=cap)


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_active_predicate_filters_before_channel_rank_and_rrf(
    mode: SearchMode,
) -> None:
    vectors = Candidates(
        [
            hit("retired", "old"),
            hit("staging", "future"),
            hit("old-flat", None),
            hit("active"),
            hit("legacy", None, source="legacy"),
        ]
    )
    reader, model = service(vectors, Documents())
    result = await reader.search(
        SearchQuery(kb_id="kb", query="q", mode=mode, fetch_k=2)
    )
    assert [item.id for item in result.chunks] == ["active", "legacy"]
    assert model.calls == (0 if mode is SearchMode.KEYWORD else 1)
    if mode is not SearchMode.KEYWORD:
        assert result.chunks[0].vector_rank == 1
    if mode is not SearchMode.DENSE:
        assert result.chunks[0].keyword_rank == 1
    if mode is SearchMode.HYBRID:
        assert result.chunks[0].fusion_score == pytest.approx(2 / 61)


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_revision_lookup_failure_never_degrades_to_other_channel(
    mode: SearchMode,
) -> None:
    documents = Documents()
    documents.failure = True
    reader, _ = service(Candidates([hit("active")]), documents)
    with pytest.raises(RevisionVisibilityError, match="visibility unavailable"):
        await reader.search(SearchQuery(kb_id="kb", query="q", mode=mode))


async def test_missing_active_mapping_key_is_not_legacy_none() -> None:
    documents = Documents()
    documents.incomplete = True
    reader, _ = service(Candidates([hit("legacy", None, source="legacy")]), documents)
    with pytest.raises(RevisionVisibilityError, match="incomplete"):
        await reader.search(SearchQuery(kb_id="kb", query="q"))


@pytest.mark.parametrize(
    "metadata",
    [
        {"document_revision": "new"},
        {"source_id": "", "document_revision": "new"},
        {"source_id": "source", "document_revision": None},
        {"source_id": "source", "document_revision": ""},
        {"source_id": "source", "document_revision": 123},
        {"kb_id": "other", "source_id": "source", "document_revision": "new"},
        {"source_id": "source" * 20, "document_revision": "new"},
    ],
)
async def test_corrupt_metadata_is_rejected_with_safe_diagnostic(metadata) -> None:
    reader, _ = service(
        Candidates([SearchHit("bad", "private body", 2, metadata), hit("ok")]),
        Documents(),
    )
    result = await reader.search(SearchQuery(kb_id="kb", query="q"))
    assert [item.id for item in result.chunks] == ["ok"]
    assert [item.reason for item in result.degradations] == [
        "invalid_revision_metadata"
    ]
    assert "private" not in str(result.degradations)


async def test_two_round_recall_fills_visible_results_without_reembedding() -> None:
    vectors = Candidates(
        [hit(f"stale-{i}", "old") for i in range(80)] + [hit("active")]
    )
    documents = Documents()
    reader, model = service(vectors, documents)
    result = await reader.search(SearchQuery(kb_id="kb", query="q", fetch_k=1))
    assert [item.id for item in result.chunks] == ["active"]
    assert vectors.limits["dense"] == [64, 2048]
    assert documents.lookups == [["source"], ["source"]]
    assert model.calls == 1


async def test_stale_congestion_has_cap_diagnostic_and_no_third_query() -> None:
    vectors = Candidates(
        [hit(f"stale-{i}", "old") for i in range(2048)] + [hit("beyond-cap")]
    )
    documents = Documents()
    reader, _ = service(vectors, documents)
    result = await reader.search(SearchQuery(kb_id="kb", query="q", fetch_k=1))
    assert result.chunks == []
    assert vectors.limits["dense"] == [64, 2048]
    assert len(documents.lookups) == 2
    assert [item.reason for item in result.degradations] == ["revision_candidate_cap"]
    # 底层重复 ID 也不能把“第二轮已到召回上限”静默变成没有诊断。
    vectors = Candidates([hit("duplicate", "old")] * 2048)
    reader, _ = service(vectors, Documents())
    result = await reader.search(SearchQuery(kb_id="kb", query="q", fetch_k=1))
    assert vectors.limits["dense"] == [64, 2048]
    assert result.chunks == []
    assert [item.reason for item in result.degradations] == ["revision_candidate_cap"]


async def test_initial_limit_equals_cap_queries_only_once() -> None:
    documents = Documents()
    limits: list[int] = []

    async def recall(limit: int) -> list[SearchHit]:
        limits.append(limit)
        return [hit(f"stale-{i}", "old") for i in range(limit)]

    result = await RevisionRecall(documents, max_candidates=64).recall(
        "kb", fetch_k=20, recall=recall
    )
    assert limits == [64]
    assert result.diagnostics == ("revision_candidate_cap",)


async def test_recall_union_never_checks_more_than_cap_even_if_ann_changes() -> None:
    documents = Documents()
    limits: list[int] = []

    async def recall(limit: int) -> list[SearchHit]:
        limits.append(limit)
        offset = 0 if len(limits) == 1 else 64
        return [
            hit(f"{i}", "old", source=f"s{i}") for i in range(offset, offset + limit)
        ]

    # 每个 source 都有旧 revision；没有 active 的 source 也不能看见 staging。
    result = await RevisionRecall(documents, max_candidates=128).recall(
        "kb", fetch_k=1, recall=recall
    )
    assert result.hits == []
    assert [len(sources) for sources in documents.lookups] == [64, 128]
    assert limits == [64, 128]


async def test_active_pointer_is_reloaded_before_second_round() -> None:
    documents = Documents()
    calls = 0

    async def recall(limit: int) -> list[SearchHit]:
        nonlocal calls
        calls += 1
        if calls == 2:
            documents.active["source"] = "future"
        return [hit("new-active", "new"), hit("future-active", "future")] + [
            hit(f"stale-{i}", "old") for i in range(70)
        ]

    result = await RevisionRecall(documents).recall("kb", fetch_k=2, recall=recall)
    assert [item.id for item in result.hits] == ["future-active"]
    assert calls == 2


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_unconfigured_reader_refuses_revision_candidates(
    mode: SearchMode,
) -> None:
    reader, _ = service(Candidates([hit("active")]), None)
    with pytest.raises(RevisionVisibilityError, match="requires DocumentStore"):
        await reader.search(SearchQuery(kb_id="kb", query="q", mode=mode))


async def test_hybrid_channel_failure_preserves_surviving_visibility_diagnostics() -> (
    None
):
    vectors = Candidates([hit("bad", "")])
    vectors.dense_failure = True
    reader, _ = service(vectors, Documents())
    result = await reader.search(
        SearchQuery(kb_id="kb", query="q", mode=SearchMode.HYBRID)
    )
    assert result.mode is SearchMode.KEYWORD
    assert [item.reason for item in result.degradations] == [
        "channel_unavailable",
        "invalid_revision_metadata",
    ]


def parent(
    pid: str, *, revision: str = "new", kb: str = "kb", source: str = "source"
) -> StoredParent:
    return StoredParent(
        RevisionKey(kb, source, revision),
        pid,
        0,
        f"parent {pid}",
        0,
        9,
        metadata={"document_revision": "forged", "chunk_start": 999},
    )


class ReverseReranker(AsyncBaseReranker):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def _scores(self, query, documents, **kwargs) -> list[float]:
        return [float(i) for i in range(len(documents))]

    async def _score(self, query, documents, **kwargs) -> list[float]:
        self.inputs = list(documents)
        return self._scores(query, documents)


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_parent_lookup_deduplicates_full_ranked_children_before_top_k(
    mode: SearchMode,
) -> None:
    documents = Documents()
    documents.parents = {pid: parent(pid) for pid in ("p1", "p2")}
    children = [hit("c1", parent="p1"), hit("c2", parent="p1"), hit("c3", parent="p2")]
    reader, _ = service(Candidates(children), documents)
    result = await reader.search(
        SearchQuery(
            kb_id="kb", query="q", mode=mode, top_k=2, return_level=ReturnLevel.PARENT
        )
    )
    assert [item.id for item in result.chunks] == ["p1", "p2"]
    assert [item.matched_child_id for item in result.chunks] == ["c1", "c3"]
    assert [item.text for item in result.chunks] == ["parent p1", "parent p2"]
    assert len(documents.parent_lookups) == 1
    assert set(documents.parent_lookups[0]) == {"p1", "p2"}
    assert result.chunks[0].metadata["document_revision"] == "new"
    assert result.chunks[0].metadata["chunk_start"] == 0
    assert result.fetched == 3


async def test_reranker_receives_children_and_parent_preserves_matched_child_scores() -> (
    None
):
    documents = Documents()
    documents.parents = {pid: parent(pid) for pid in ("p1", "p2")}
    children = [hit("c1", parent="p1"), hit("c2", parent="p1"), hit("c3", parent="p2")]
    reranker = ReverseReranker()
    reader, _ = service(Candidates(children), documents, reranker=reranker)
    result = await reader.search(
        SearchQuery(kb_id="kb", query="q", top_k=2, return_level=ReturnLevel.PARENT)
    )
    assert reranker.inputs == ["child c1", "child c2", "child c3"]
    assert [
        (item.id, item.matched_child_id, item.score, item.vector_rank)
        for item in result.chunks
    ] == [
        ("p2", "c3", 2.0, 3),
        ("p1", "c2", 1.0, 2),
    ]
    assert result.reranked


@pytest.mark.parametrize(
    "fault", ["missing", "revision", "kb", "source", "id", "connection"]
)
async def test_parent_failure_returns_verified_children_with_diagnostic(
    fault: str,
) -> None:
    documents = Documents()
    if fault == "connection":
        documents.parent_failure = True
    elif fault != "missing":
        documents.parents["p1"] = parent(
            "wrong" if fault == "id" else "p1",
            revision="old" if fault == "revision" else "new",
            kb="other" if fault == "kb" else "kb",
            source="other" if fault == "source" else "source",
        )
    reader, _ = service(Candidates([hit("c1", parent="p1")]), documents)
    result = await reader.search(
        SearchQuery(kb_id="kb", query="q", return_level=ReturnLevel.PARENT)
    )
    assert [item.id for item in result.chunks] == ["c1"]
    assert result.chunks[0].matched_child_id is None
    assert len(result.degradations) == 1
    assert result.degradations[0].stage.value == "parent"
    assert "secret-password" not in str(result.degradations)


async def test_default_child_and_legacy_parent_queries_do_not_fetch_parents() -> None:
    documents = Documents()
    reader, _ = service(Candidates([hit("active", parent="p1")]), documents)
    child = await reader.search(SearchQuery(kb_id="kb", query="q"))
    assert child.chunks[0].id == "active"
    reader, _ = service(Candidates([hit("legacy", None, source="legacy")]), documents)
    legacy = await reader.search(
        SearchQuery(kb_id="kb", query="q", return_level=ReturnLevel.PARENT)
    )
    assert legacy.chunks[0].id == "legacy"
    assert documents.parent_lookups == []


async def test_real_memory_staging_activation_and_retirement_are_visible_atomically() -> (
    None
):
    documents, vectors, model = (
        InMemoryDocumentStore(),
        AsyncInMemoryVectorStore(),
        Embedding(),
    )
    writer = RevisionStager(
        document_store=documents, vector_store=vectors, embedding_model=model
    )
    reader = RetrievalService(
        embedding_model=model, vector_store=vectors, document_store=documents
    )

    def plan(revision: str, text: str):
        return IndexPlanner().plan(
            HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(3)).split(
                NormalizedDocument(markdown=text)
            ),
            key=RevisionKey("kb", "source", revision),
            source="fixture.md",
            file_type="md",
        )

    old, new = plan("old", "oldOLD"), plan("new", "newNEW")
    await writer.stage(old, embedding_dim=2)
    query = SearchQuery(kb_id="kb", query="q", return_level=ReturnLevel.PARENT)
    assert (await reader.search(query)).chunks == []
    await documents.activate_revision(old.key)
    assert [item.text for item in (await reader.search(query)).chunks] == ["oldOLD"]
    await writer.stage(new, embedding_dim=2)
    assert [item.text for item in (await reader.search(query)).chunks] == ["oldOLD"]
    await documents.activate_revision(new.key)
    assert [item.text for item in (await reader.search(query)).chunks] == ["newNEW"]
    assert await vectors.count("kb") == 4


@pytest.mark.parametrize("fault", ["missing", "revision", "kb", "source", "id"])
async def test_unusable_shared_parent_keeps_each_verified_child(fault: str) -> None:
    documents = Documents()
    if fault != "missing":
        documents.parents["p1"] = parent(
            "wrong" if fault == "id" else "p1",
            revision="old" if fault == "revision" else "new",
            kb="other" if fault == "kb" else "kb",
            source="other" if fault == "source" else "source",
        )
    children = [hit("c1", parent="p1"), hit("c2", parent="p1")]
    reader, _ = service(Candidates(children), documents)
    result = await reader.search(
        SearchQuery(kb_id="kb", query="q", top_k=2, return_level=ReturnLevel.PARENT)
    )
    assert [item.id for item in result.chunks] == ["c1", "c2"]
    assert [item.vector_rank for item in result.chunks] == [1, 2]
    assert all(item.matched_child_id is None for item in result.chunks)
    assert documents.parent_lookups == [["p1"]]
    assert [item.reason for item in result.degradations] == ["invalid_parent"]
    assert result.fetched == 2


@pytest.mark.parametrize("wrong_first", [True, False])
async def test_parent_deduplication_never_discards_a_different_source_child(
    wrong_first: bool,
) -> None:
    documents = Documents()
    documents.active["other"] = "new"
    documents.parents["p1"] = parent("p1")
    wrong = hit("wrong", source="other", parent="p1")
    correct = hit("correct", parent="p1")
    reader, _ = service(
        Candidates([wrong, correct] if wrong_first else [correct, wrong]), documents
    )
    result = await reader.search(
        SearchQuery(kb_id="kb", query="q", top_k=2, return_level=ReturnLevel.PARENT)
    )
    assert [item.id for item in result.chunks] == (
        ["wrong", "p1"] if wrong_first else ["p1", "wrong"]
    )
    expanded = next(item for item in result.chunks if item.id == "p1")
    assert expanded.matched_child_id == "correct"
    assert expanded.metadata["source_id"] == "source"
    assert [item.reason for item in result.degradations] == ["invalid_parent"]
    assert documents.parent_lookups == [["p1"]]


async def test_parent_expansion_preserves_ranking_and_deduplicates_before_top_k() -> (
    None
):
    documents = Documents()
    documents.parents = {pid: parent(pid) for pid in ("p1", "p2")}
    children = [
        hit("missing-1", parent="missing"),
        hit("p1-child-1", parent="p1"),
        hit("p1-child-2", parent="p1"),
        hit("missing-2", parent="missing"),
        hit("p2-child", parent="p2"),
    ]
    reader, _ = service(Candidates(children), documents)
    result = await reader.search(
        SearchQuery(kb_id="kb", query="q", top_k=4, return_level=ReturnLevel.PARENT)
    )
    assert [item.id for item in result.chunks] == ["missing-1", "p1", "missing-2", "p2"]
    assert [item.vector_rank for item in result.chunks] == [1, 2, 4, 5]
    assert [item.matched_child_id for item in result.chunks] == [
        None,
        "p1-child-1",
        None,
        "p2-child",
    ]
    assert documents.parent_lookups == [["missing", "p1", "p2"]]
    assert [item.reason for item in result.degradations] == ["invalid_parent"]
    assert result.fetched == 5
    assert [item.metadata["parent_id"] for item in children] == [
        "missing",
        "p1",
        "p1",
        "missing",
        "p2",
    ]
