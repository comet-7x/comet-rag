"""检索用例：召回范围、重排、降级。

重排的降级路径尤其要测：它是"服务还在但结果变差"的典型 ——
不测的话，线上重排挂了半个月都可能没人发现。
"""

from __future__ import annotations

from typing import Any

import pytest

from comet_rag.infrastructure.knowledge_base import (
    EmbeddingModelChanged,
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.infrastructure.providers.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.providers.reranker.base import BaseReranker
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from comet_rag.ports import CollectionSchemaMismatch, VectorRecord
from comet_rag.services import retrieval as retrieval_module
from comet_rag.services.knowledge_base import KnowledgeBaseService, KnowledgeBaseSpec
from comet_rag.services.retrieval import (
    HybridRecallFailed,
    KeywordSearchUnavailable,
    RecallChannel,
    RetrievalService,
    RetrievalStage,
    SearchMode,
    SearchQuery,
)

DIM = 3
KB = "kb-search"
OTHER_KB = "kb-other"


# ── 测试替身 ───────────────────────────────────────────────────────────────


class KeywordEmbeddingModel(BaseEmbeddingModel):
    """把文本映射到三个关键词维度上，让"谁该排第一"可预测。

    维度含义：[包含"苹果", 包含"香蕉", 常数]
    """

    def __init__(self) -> None:
        self.calls = 0

    def _vector(self, text: str) -> list[float]:
        return [
            1.0 if "苹果" in text else 0.0,
            1.0 if "香蕉" in text else 0.0,
            0.1,
        ]

    def _embed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        return self._vector(str(data))

    async def _aembed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        return self._vector(str(data))

    async def close_client(self) -> None:  # pragma: no cover
        return None


class ReversingReranker(BaseReranker):
    """把向量召回的顺序整个倒过来 —— 重排是否真的生效一目了然。"""

    def __init__(self) -> None:
        self.calls = 0
        self.last_documents: list[str] = []
        self.failure: Exception | None = None
        self.wrong_length = False

    def _score(self, query, documents, **kwargs) -> list[float]:  # pragma: no cover
        raise NotImplementedError

    async def _ascore(self, query, documents, **kwargs) -> list[float]:
        self.calls += 1
        self.last_documents = list(documents)
        if self.failure is not None:
            raise self.failure
        if self.wrong_length:
            return [1.0]
        return [float(i) for i in range(len(self.last_documents))]


# ── 夹具 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def model() -> KeywordEmbeddingModel:
    return KeywordEmbeddingModel()


@pytest.fixture
def reranker() -> ReversingReranker:
    return ReversingReranker()


@pytest.fixture
async def store(model: KeywordEmbeddingModel) -> InMemoryVectorStore:
    vs = InMemoryVectorStore()
    await vs.aensure_collection(KB, dim=DIM)
    await vs.aensure_collection(OTHER_KB, dim=DIM)

    async def add(kb: str, rid: str, text: str, **metadata: Any) -> None:
        await vs.aupsert(
            kb,
            [
                VectorRecord(
                    id=rid,
                    text=text,
                    embedding=await model.aembed(text),
                    metadata={"kb_id": kb, **metadata},
                )
            ],
        )

    await add(KB, "apple", "苹果的营养价值", source="a.docx", lang="zh")
    await add(KB, "banana", "香蕉的种植方法", source="b.docx", lang="zh")
    await add(KB, "both", "苹果和香蕉的对比", source="c.docx", lang="en")
    await add(OTHER_KB, "secret", "苹果 —— 别的知识库的机密内容")
    return vs


def service(model, store, reranker=None) -> RetrievalService:
    return RetrievalService(
        embedding_model=model,
        vector_store=store,
        keyword_search=store,
        reranker=reranker,
    )


# ── 召回 ───────────────────────────────────────────────────────────────────


async def test_most_relevant_chunk_ranks_first(model, store) -> None:
    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="香蕉怎么种", top_k=3)
    )

    assert result.chunks[0].id == "banana"
    assert result.chunks[0].score >= result.chunks[-1].score


async def test_results_are_sorted_descending(model, store) -> None:
    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )

    scores = [c.score for c in result.chunks]
    assert scores == sorted(scores, reverse=True)


async def test_search_never_crosses_knowledge_bases(model, store) -> None:
    """**最严重的隔离事故**：别的知识库的内容不得出现在结果里（spec A5）。"""
    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=10)
    )

    assert all(c.metadata["kb_id"] == KB for c in result.chunks)
    assert "secret" not in {c.id for c in result.chunks}


async def test_top_k_limits_results(model, store) -> None:
    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=1)
    )

    assert len(result.chunks) == 1


async def test_empty_knowledge_base_returns_nothing(model, store) -> None:
    await store.aensure_collection("kb-empty", dim=DIM)

    result = await service(model, store).search(
        SearchQuery(kb_id="kb-empty", query="苹果")
    )

    assert result.chunks == []
    assert result.fetched == 0


async def test_search_rejects_same_dimension_from_different_model(model, store) -> None:
    repo = InMemoryKnowledgeBaseRepository()
    original = KnowledgeBaseService(
        repository=repo,
        vector_store=store,
        embedding_model="original-model",
        embedding_dim=DIM,
    )
    await original.create(KnowledgeBaseSpec(kb_id=KB))
    reopened = KnowledgeBaseService(
        repository=repo,
        vector_store=store,
        embedding_model="different-model",
        embedding_dim=DIM,
    )
    guarded = RetrievalService(
        embedding_model=model,
        vector_store=store,
        keyword_search=store,
        knowledge_base=reopened,
    )

    with pytest.raises(EmbeddingModelChanged):
        await guarded.search(
            SearchQuery(kb_id=KB, query="苹果", mode=SearchMode.HYBRID)
        )


async def test_results_carry_metadata(model, store) -> None:
    result = await service(model, store).search(SearchQuery(kb_id=KB, query="苹果"))

    assert result.chunks[0].metadata["source"].endswith(".docx")


async def test_filter_narrows_results(model, store) -> None:
    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=10, filter={"lang": "en"})
    )

    assert [c.id for c in result.chunks] == ["both"]


async def test_empty_filter_is_treated_as_no_filter(model, store) -> None:
    """调用方传 `{}` 是"我没有过滤条件"，不该被当成"匹配所有键"。"""
    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=10, filter={})
    )

    assert len(result.chunks) == 3


# ── 检索模式 ───────────────────────────────────────────────────────────────


async def test_default_mode_remains_dense(model, store, monkeypatch) -> None:
    async def unexpected_keyword_call(*args, **kwargs):
        raise AssertionError("默认 dense 不应调用关键词通道")

    monkeypatch.setattr(store, "asearch_keywords", unexpected_keyword_call)

    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )

    assert result.mode is SearchMode.DENSE
    assert result.channels == (RecallChannel.DENSE,)
    assert all(chunk.vector_score is not None for chunk in result.chunks)
    assert all(chunk.keyword_score is None for chunk in result.chunks)
    assert all(chunk.fusion_score is None for chunk in result.chunks)


async def test_keyword_mode_skips_embedding_and_exposes_keyword_scores(
    model, store
) -> None:
    model.calls = 0

    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3, mode=SearchMode.KEYWORD)
    )

    assert model.calls == 0
    assert result.mode is SearchMode.KEYWORD
    assert result.channels == (RecallChannel.KEYWORD,)
    assert {chunk.id for chunk in result.chunks} == {"apple", "both"}
    assert all(chunk.score == chunk.keyword_score for chunk in result.chunks)
    assert [chunk.keyword_rank for chunk in result.chunks] == list(
        range(1, len(result.chunks) + 1)
    )
    assert all(chunk.vector_score is None for chunk in result.chunks)


async def test_hybrid_mode_fuses_both_channels_before_top_k(model, store) -> None:
    result = await service(model, store).search(
        SearchQuery(
            kb_id=KB,
            query="苹果",
            top_k=3,
            fetch_k=3,
            mode=SearchMode.HYBRID,
        )
    )

    assert result.mode is SearchMode.HYBRID
    assert result.channels == (RecallChannel.DENSE, RecallChannel.KEYWORD)
    assert {chunk.id for chunk in result.chunks} == {"apple", "both", "banana"}
    assert all(chunk.score == chunk.fusion_score for chunk in result.chunks)
    shared = [chunk for chunk in result.chunks if chunk.id in {"apple", "both"}]
    assert all(chunk.vector_score is not None for chunk in shared)
    assert all(chunk.keyword_score is not None for chunk in shared)


async def test_hybrid_rerank_preserves_channel_and_fusion_diagnostics(
    model, store, reranker
) -> None:
    result = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3, mode=SearchMode.HYBRID)
    )

    assert result.reranked is True
    assert all(chunk.fusion_score is not None for chunk in result.chunks)
    assert any(chunk.score != chunk.fusion_score for chunk in result.chunks)
    assert any(chunk.keyword_rank is not None for chunk in result.chunks)


@pytest.mark.parametrize("mode", [SearchMode.KEYWORD, SearchMode.HYBRID])
async def test_keyword_capability_is_required_only_by_modes_that_use_it(
    model, store, mode
) -> None:
    without_keyword = RetrievalService(
        embedding_model=model,
        vector_store=store,
    )

    with pytest.raises(KeywordSearchUnavailable, match="KeywordSearchPort"):
        await without_keyword.search(SearchQuery(kb_id=KB, query="苹果", mode=mode))


async def test_hybrid_uses_same_fetch_k_and_filter_for_each_channel(
    model, store, monkeypatch
) -> None:
    calls: dict[str, tuple[int, object]] = {}
    dense_search = store.asearch
    keyword_search = store.asearch_keywords

    async def recording_dense(*args, top_k=5, filter=None, **kwargs):
        calls["dense"] = (top_k, filter)
        return await dense_search(*args, top_k=top_k, filter=filter, **kwargs)

    async def recording_keyword(*args, top_k=5, filter=None, **kwargs):
        calls["keyword"] = (top_k, filter)
        return await keyword_search(*args, top_k=top_k, filter=filter, **kwargs)

    monkeypatch.setattr(store, "asearch", recording_dense)
    monkeypatch.setattr(store, "asearch_keywords", recording_keyword)

    await service(model, store).search(
        SearchQuery(
            kb_id=KB,
            query="苹果",
            top_k=1,
            fetch_k=2,
            filter={"lang": "zh"},
            mode=SearchMode.HYBRID,
        )
    )

    assert calls == {
        "dense": (2, {"lang": "zh"}),
        "keyword": (2, {"lang": "zh"}),
    }


# ── hybrid 通道降级 ────────────────────────────────────────────────────────


async def test_hybrid_dense_failure_degrades_to_keyword_and_logs(
    model, store, monkeypatch
) -> None:
    class RecordingLogger:
        def __init__(self) -> None:
            self.exceptions: list[BaseException] = []
            self.messages: list[str] = []

        def opt(self, *, exception):
            self.exceptions.append(exception)
            return self

        def warning(self, message: str) -> None:
            self.messages.append(message)

    log = RecordingLogger()

    async def fail_dense(*args, **kwargs):
        raise TimeoutError("embedding timeout with private endpoint")

    monkeypatch.setattr(store, "asearch", fail_dense)
    monkeypatch.setattr(retrieval_module, "logger", log)

    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", mode=SearchMode.HYBRID)
    )

    assert result.mode is SearchMode.KEYWORD
    assert result.channels == (RecallChannel.KEYWORD,)
    assert result.chunks
    assert result.degradations[0].stage is RetrievalStage.DENSE
    assert result.degradations[0].reason == "channel_unavailable"
    assert result.degradations[0].error_type == "TimeoutError"
    assert len(log.exceptions) == 1
    assert "stage=dense" in log.messages[0]
    assert "降级继续" in log.messages[0]
    assert all(
        "private endpoint" not in (value or "")
        for value in result.degradations[0].to_dict().values()
    )


async def test_hybrid_keyword_failure_degrades_to_dense(
    model, store, monkeypatch
) -> None:
    async def fail_keyword(*args, **kwargs):
        raise ConnectionError("keyword backend unavailable")

    monkeypatch.setattr(store, "asearch_keywords", fail_keyword)

    result = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", mode=SearchMode.HYBRID)
    )

    assert result.mode is SearchMode.DENSE
    assert result.channels == (RecallChannel.DENSE,)
    assert result.chunks
    assert result.degradations[0].stage is RetrievalStage.KEYWORD
    assert all(chunk.keyword_score is None for chunk in result.chunks)


async def test_hybrid_raises_one_error_when_both_channels_fail(
    model, store, monkeypatch
) -> None:
    class RecordingLogger:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def opt(self, *, exception):
            return self

        def warning(self, message: str) -> None:
            self.messages.append(message)

    log = RecordingLogger()

    async def fail_dense(*args, **kwargs):
        raise TimeoutError("dense timeout")

    async def fail_keyword(*args, **kwargs):
        raise ConnectionError("keyword connection reset")

    monkeypatch.setattr(store, "asearch", fail_dense)
    monkeypatch.setattr(store, "asearch_keywords", fail_keyword)
    monkeypatch.setattr(retrieval_module, "logger", log)

    with pytest.raises(HybridRecallFailed) as caught:
        await service(model, store).search(
            SearchQuery(kb_id=KB, query="苹果", mode=SearchMode.HYBRID)
        )

    assert [item.stage for item in caught.value.degradations] == [
        RetrievalStage.DENSE,
        RetrievalStage.KEYWORD,
    ]
    assert "dense timeout" not in str(caught.value)
    assert "keyword connection reset" not in str(caught.value)
    assert len(log.messages) == 2
    assert all("请求失败" in message for message in log.messages)
    assert all("降级继续" not in message for message in log.messages)


@pytest.mark.parametrize(
    "failure",
    [
        CollectionSchemaMismatch(KB, ["缺少 BM25 function"]),
        ValueError("invalid filter"),
    ],
)
async def test_hybrid_does_not_hide_schema_or_parameter_errors(
    model, store, monkeypatch, failure
) -> None:
    async def fail_keyword(*args, **kwargs):
        raise failure

    monkeypatch.setattr(store, "asearch_keywords", fail_keyword)

    with pytest.raises(type(failure)) as caught:
        await service(model, store).search(
            SearchQuery(kb_id=KB, query="苹果", mode=SearchMode.HYBRID)
        )

    assert caught.value is failure


async def test_explicit_keyword_mode_never_falls_back_to_dense(
    model, store, monkeypatch
) -> None:
    model.calls = 0

    async def fail_keyword(*args, **kwargs):
        raise TimeoutError("keyword timeout")

    monkeypatch.setattr(store, "asearch_keywords", fail_keyword)

    with pytest.raises(TimeoutError, match="keyword timeout"):
        await service(model, store).search(
            SearchQuery(kb_id=KB, query="苹果", mode=SearchMode.KEYWORD)
        )

    assert model.calls == 0


async def test_explicit_dense_mode_never_falls_back_to_keyword(
    model, store, monkeypatch
) -> None:
    keyword_called = False

    async def fail_dense(*args, **kwargs):
        raise TimeoutError("dense timeout")

    async def record_keyword(*args, **kwargs):
        nonlocal keyword_called
        keyword_called = True
        return []

    monkeypatch.setattr(store, "asearch", fail_dense)
    monkeypatch.setattr(store, "asearch_keywords", record_keyword)

    with pytest.raises(TimeoutError, match="dense timeout"):
        await service(model, store).search(
            SearchQuery(kb_id=KB, query="苹果", mode=SearchMode.DENSE)
        )

    assert keyword_called is False


# ── fetch_k 与 top_k ───────────────────────────────────────────────────────


def test_fetch_k_defaults_to_a_multiple_of_top_k() -> None:
    """两者相等时重排只能在最终结果内部调序，捞不回被召回漏掉的文档 ——
    那恰恰是重排最大的价值。"""
    assert SearchQuery(kb_id="k", query="q", top_k=5).effective_fetch_k() == 20
    assert SearchQuery(kb_id="k", query="q", top_k=10).effective_fetch_k() == 40


def test_fetch_k_is_never_smaller_than_top_k() -> None:
    """配反了（fetch_k < top_k）会让返回数量莫名其妙地少，直接纠正。"""
    q = SearchQuery(kb_id="k", query="q", top_k=10, fetch_k=3)

    assert q.effective_fetch_k() == 10


async def test_reranker_receives_fetch_k_candidates_not_top_k(
    model, store, reranker
) -> None:
    result = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=1, fetch_k=10)
    )

    assert len(reranker.last_documents) == 3, "应把全部候选送去重排"
    assert result.fetched == 3
    assert len(result.chunks) == 1, "但只返回 top_k 个"


# ── 重排 ───────────────────────────────────────────────────────────────────


async def test_rerank_changes_order(model, store, reranker) -> None:
    without = await service(model, store).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )
    with_rerank = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )

    assert with_rerank.reranked is True
    assert [c.id for c in with_rerank.chunks] != [c.id for c in without.chunks]


async def test_rerank_preserves_vector_score(model, store, reranker) -> None:
    """保留重排前的分数，才能对比两者差异、判断重排到底有没有帮上忙。"""
    result = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )

    assert all(c.vector_score is not None for c in result.chunks)
    assert any(c.score != c.vector_score for c in result.chunks)


async def test_no_reranker_configured_is_not_an_error(model, store) -> None:
    """只有 embedding 服务时链路也要能跑通。"""
    result = await service(model, store).search(SearchQuery(kb_id=KB, query="苹果"))

    assert result.reranked is False
    assert result.chunks


async def test_rerank_can_be_disabled_per_query(model, store, reranker) -> None:
    result = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", rerank=False)
    )

    assert result.reranked is False
    assert reranker.calls == 0


async def test_rerank_failure_degrades_instead_of_raising(
    model, store, reranker
) -> None:
    """检索是读路径：给出稍差的结果远好过给不出结果（spec S4-5）。"""
    reranker.failure = TimeoutError("重排服务超时")

    result = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3, mode=SearchMode.HYBRID)
    )

    assert result.reranked is False, "降级必须在结果里可见"
    assert result.chunks, "降级后仍要返回向量召回结果"
    assert result.degradations[-1].stage is RetrievalStage.RERANKER
    assert result.degradations[-1].reason == "request_failed"
    assert all(chunk.fusion_score is not None for chunk in result.chunks)


async def test_misaligned_rerank_scores_degrade(model, store, reranker) -> None:
    """分数与候选对不上时若强行 zip，会把分数张冠李戴到别的文档上 ——
    那比不重排更糟，因为错误是隐形的。"""
    reranker.wrong_length = True

    result = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )

    assert result.reranked is False
    assert len(result.chunks) == 3
    # BaseReranker 在返回给 Service 前就拒绝数量不一致，因此表现为请求失败。
    assert result.degradations[-1].reason == "request_failed"


async def test_rerank_is_skipped_when_nothing_recalled(model, store, reranker) -> None:
    await store.aensure_collection("kb-empty", dim=DIM)

    await service(model, store, reranker).search(
        SearchQuery(kb_id="kb-empty", query="苹果")
    )

    assert reranker.calls == 0, "没有候选还去调重排是白花钱"


# ── 请求校验 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kb_id": "", "query": "q"},
        {"kb_id": "k", "query": ""},
        {"kb_id": "k", "query": "q", "top_k": 0},
        {"kb_id": "k", "query": "q", "top_k": 101},
        {"kb_id": "k", "query": "q", "mode": "semantic"},
    ],
)
def test_invalid_query_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SearchQuery(**kwargs)
