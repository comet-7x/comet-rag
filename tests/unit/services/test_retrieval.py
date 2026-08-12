"""检索用例：召回范围、重排、降级。

重排的降级路径尤其要测：它是"服务还在但结果变差"的典型 ——
不测的话，线上重排挂了半个月都可能没人发现。
"""

from __future__ import annotations

from typing import Any

import pytest

from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.models.reranker.base import BaseReranker
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore, VectorRecord
from comet_rag.services.retrieval import RetrievalService, SearchQuery

DIM = 3
KB = "kb-search"
OTHER_KB = "kb-other"


# ── 测试替身 ───────────────────────────────────────────────────────────────


class KeywordEmbeddingModel(BaseEmbeddingModel):
    """把文本映射到三个关键词维度上，让"谁该排第一"可预测。

    维度含义：[包含"苹果", 包含"香蕉", 常数]
    """

    def _vector(self, text: str) -> list[float]:
        return [
            1.0 if "苹果" in text else 0.0,
            1.0 if "香蕉" in text else 0.0,
            0.1,
        ]

    def embed(self, data, **kwargs) -> list[float]:
        return self._vector(str(data))

    async def _aembed(self, data, **kwargs) -> list[float]:
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

    def score(self, query, documents, **kwargs) -> list[float]:  # pragma: no cover
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
        embedding_model=model, vector_store=store, reranker=reranker
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
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )

    assert result.reranked is False, "降级必须在结果里可见"
    assert result.chunks, "降级后仍要返回向量召回结果"


async def test_misaligned_rerank_scores_degrade(model, store, reranker) -> None:
    """分数与候选对不上时若强行 zip，会把分数张冠李戴到别的文档上 ——
    那比不重排更糟，因为错误是隐形的。"""
    reranker.wrong_length = True

    result = await service(model, store, reranker).search(
        SearchQuery(kb_id=KB, query="苹果", top_k=3)
    )

    assert result.reranked is False
    assert len(result.chunks) == 3


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
    ],
)
def test_invalid_query_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SearchQuery(**kwargs)
