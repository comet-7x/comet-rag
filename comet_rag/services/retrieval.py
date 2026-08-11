"""检索用例：query → 向量召回 → 重排 → 结果。

## 为什么 `fetch_k` 与 `top_k` 是两个参数

向量召回快但粗（余弦相似度只看语义相近），重排慢但准（交叉编码器真读
query 与文档的关系）。所以标准做法是**粗召回一批、精排出一小撮**：
`fetch_k=50` 交给重排，最终返回 `top_k=5`。

若两者相等，重排就只能在最终结果内部调整顺序，**捞不回被向量召回漏掉的
那些文档** —— 重排最大的价值恰恰在于此。

## 重排是可选的

没配 reranker 时直接返回向量召回的结果，不报错。这既是为了让链路在
只有 embedding 服务时也能跑通，也是分级降级（spec S4-5）的落点：
重排服务抖动时先砍掉它，检索质量下降但不中断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from comet_rag.core.logging import logger
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.models.reranker.base import BaseReranker
from comet_rag.infrastructure.vectorstore import BaseVectorStore, Filter


class SearchQuery(BaseModel):
    kb_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, gt=0, le=100)
    fetch_k: int | None = Field(
        default=None,
        gt=0,
        le=500,
        description="送进重排的候选数。缺省为 top_k 的 4 倍（至少 20）。",
    )
    filter: dict[str, Any] | None = None
    rerank: bool = Field(default=True, description="是否重排（未配置时自动跳过）")

    def effective_fetch_k(self) -> int:
        if self.fetch_k is not None:
            return max(self.fetch_k, self.top_k)
        return max(self.top_k * 4, 20)


@dataclass(slots=True)
class RetrievedChunk:
    id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 重排前的向量相似度。重排发生时保留它，便于对比两者差异、调参。
    vector_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "score": self.score,
            "metadata": self.metadata,
            "vector_score": self.vector_score,
        }


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    #: 重排是否真的执行了。为 False 时要么没配 reranker，要么被降级/显式关闭。
    reranked: bool
    fetched: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks": [c.to_dict() for c in self.chunks],
            "reranked": self.reranked,
            "fetched": self.fetched,
        }


class RetrievalService:
    def __init__(
        self,
        *,
        embedding_model: BaseEmbeddingModel,
        vector_store: BaseVectorStore,
        reranker: BaseReranker | None = None,
    ) -> None:
        self._embedding_model = embedding_model
        self._vector_store = vector_store
        self._reranker = reranker

    async def search(self, query: SearchQuery) -> RetrievalResult:
        candidates = await self._recall(query)
        if not candidates:
            return RetrievalResult(chunks=[], reranked=False, fetched=0)

        should_rerank = query.rerank and self._reranker is not None
        if not should_rerank:
            return RetrievalResult(
                chunks=candidates[: query.top_k],
                reranked=False,
                fetched=len(candidates),
            )

        chunks, did_rerank = await self._rerank(self._reranker, query.query, candidates)
        return RetrievalResult(
            chunks=chunks[: query.top_k],
            reranked=did_rerank,
            fetched=len(candidates),
        )

    async def _recall(self, query: SearchQuery) -> list[RetrievedChunk]:
        embedding = await self._embedding_model.aembed(query.query)
        hits = await self._vector_store.asearch(
            query.kb_id,
            embedding,
            top_k=query.effective_fetch_k(),
            filter=_normalize_filter(query.filter),
        )
        return [
            RetrievedChunk(
                id=hit.id,
                text=hit.text,
                score=hit.score,
                metadata=hit.metadata,
                vector_score=hit.score,
            )
            for hit in hits
        ]

    @staticmethod
    async def _rerank(
        reranker: BaseReranker, query: str, candidates: list[RetrievedChunk]
    ) -> tuple[list[RetrievedChunk], bool]:
        """重排并返回 `(结果, 是否真的重排了)`。

        重排失败时**降级返回向量召回结果**，而不是让整个查询失败 ——
        检索是读路径，给出稍差的结果远好过给不出结果。降级必须留下日志，
        否则线上质量下滑无人察觉（spec S4-5）。

        显式返回布尔而非让调用方比对列表身份：后者能跑通但极易在重构中失效。
        reranker 由调用方传入而非从 self 取，省掉一个"此处它必不为 None"的断言。
        """
        try:
            scores = await reranker.ascore(query, [c.text for c in candidates])
        except Exception as exc:  # noqa: BLE001 —— 任何重排故障都降级
            logger.warning(f"重排失败，降级为向量召回结果：{exc!r}")
            return candidates, False

        if len(scores) != len(candidates):
            logger.warning(
                f"重排返回 {len(scores)} 个分数但候选有 {len(candidates)} 个，"
                f"结果不可对齐，降级为向量召回结果"
            )
            return candidates, False

        rescored = [
            RetrievedChunk(
                id=c.id,
                text=c.text,
                score=score,
                metadata=c.metadata,
                vector_score=c.vector_score,
            )
            for c, score in zip(candidates, scores, strict=True)
        ]
        # id 作次级键，保证同分时顺序稳定
        rescored.sort(key=lambda c: (-c.score, c.id))
        return rescored, True


def _normalize_filter(filter: dict[str, Any] | None) -> Filter | None:
    """空 dict 视为无过滤，避免调用方传 `{}` 时被当成"匹配所有键"。"""
    return filter or None


__all__ = [
    "RetrievalResult",
    "RetrievalService",
    "RetrievedChunk",
    "SearchQuery",
]
