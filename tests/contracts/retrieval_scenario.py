from __future__ import annotations

from typing import Any

from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.ports import AsyncKeywordSearchPort, AsyncVectorStore, VectorRecord
from comet_rag.services.retrieval import RetrievalService, SearchMode, SearchQuery


class _FixedQueryEmbedding(AsyncBaseEmbeddingModel):
    """把语义改写稳定映射到固定向量，隔离真实 embedding 服务的波动。"""

    async def _embed(self, text: str, /, **kwargs: Any) -> list[float]:
        if "弯月形" in text:
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0, 0.0]


async def assert_retrieval_modes(
    store: AsyncVectorStore,
    keyword_search: AsyncKeywordSearchPort,
) -> None:
    """两路召回、RRF 与结构化过滤必须在同一链路工作，且不依赖真实模型。"""
    kb = "kb-real-hybrid"
    await store.ensure_collection(kb, dim=4)
    await store.upsert(
        kb,
        [
            VectorRecord(
                id="semantic",
                text="香蕉适合温暖气候种植",
                embedding=[1.0, 0.0, 0.0, 0.0],
                metadata={"language": "zh", "kind": "fruit"},
            ),
            VectorRecord(
                id="exact",
                text="Rust TraitObject memory layout",
                embedding=[0.0, 1.0, 0.0, 0.0],
                metadata={"language": "en", "kind": "code"},
            ),
            VectorRecord(
                id="noise",
                text="数据库连接池容量规划",
                embedding=[0.0, 0.0, 1.0, 0.0],
                metadata={"language": "zh", "kind": "ops"},
            ),
        ],
    )
    service = RetrievalService(
        embedding_model=_FixedQueryEmbedding(),
        vector_store=store,
        keyword_search=keyword_search,
    )

    dense = await service.search(
        SearchQuery(
            kb_id=kb,
            query="弯月形水果如何栽培",
            mode=SearchMode.DENSE,
            rerank=False,
            top_k=1,
        )
    )
    assert [chunk.id for chunk in dense.chunks] == ["semantic"]
    assert dense.chunks[0].vector_score is not None

    keyword = await service.search(
        SearchQuery(
            kb_id=kb,
            query="TraitObject",
            mode=SearchMode.KEYWORD,
            filter={"language": "en"},
            rerank=False,
            top_k=1,
        )
    )
    assert [chunk.id for chunk in keyword.chunks] == ["exact"]
    assert keyword.chunks[0].keyword_score is not None

    hybrid = await service.search(
        SearchQuery(
            kb_id=kb,
            query="TraitObject 弯月形",
            mode=SearchMode.HYBRID,
            rerank=False,
            top_k=3,
        )
    )
    assert {chunk.id for chunk in hybrid.chunks} >= {"semantic", "exact"}
    assert hybrid.mode is SearchMode.HYBRID
    assert hybrid.chunks[0].fusion_score is not None
