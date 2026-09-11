"""编排向量召回、可选重排与降级。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, Field

from comet_rag.core.degradation import DegradationController
from comet_rag.core.logging import logger
from comet_rag.engines.retrieval import FusedHit, reciprocal_rank_fusion
from comet_rag.ports import (
    CollectionNotFound,
    CollectionSchemaMismatch,
    DimensionMismatch,
    EmbeddingPort,
    Filter,
    KeywordSearchPort,
    RerankDocument,
    RerankerPort,
    SearchHit,
    VectorSearchPort,
)
from comet_rag.services.knowledge_base import KnowledgeBaseService


class SearchMode(StrEnum):
    DENSE = "dense"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class RecallChannel(StrEnum):
    DENSE = "dense"
    KEYWORD = "keyword"


class RetrievalStage(StrEnum):
    DENSE = "dense"
    KEYWORD = "keyword"
    RERANKER = "reranker"


class KeywordSearchUnavailable(RuntimeError):
    """请求需要关键词召回，但组合根没有提供对应能力。"""


@dataclass(frozen=True, slots=True)
class RetrievalDegradation:
    """不暴露异常消息的安全降级诊断。"""

    stage: RetrievalStage
    reason: str
    error_type: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "stage": self.stage,
            "reason": self.reason,
            "error_type": self.error_type,
        }


class HybridRecallFailed(RuntimeError):
    """hybrid 的两个召回通道均不可用。"""

    def __init__(self, degradations: tuple[RetrievalDegradation, ...]) -> None:
        self.degradations = degradations
        summary = ", ".join(
            f"{item.stage.value}:{item.error_type or item.reason}"
            for item in degradations
        )
        super().__init__(f"hybrid 召回的所有通道均失败（{summary}）")


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
    mode: SearchMode = Field(
        default=SearchMode.DENSE,
        description="召回模式；默认 dense 保持既有行为。",
    )

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
    keyword_score: float | None = None
    fusion_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "score": self.score,
            "metadata": self.metadata,
            "vector_score": self.vector_score,
            "keyword_score": self.keyword_score,
            "fusion_score": self.fusion_score,
            "vector_rank": self.vector_rank,
            "keyword_rank": self.keyword_rank,
        }


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    #: 重排是否真的执行了。为 False 时要么没配 reranker，要么被降级/显式关闭。
    reranked: bool
    fetched: int
    #: 实际生效的 top_k；降级到 L2 时会小于请求值。
    #: 不暴露它的话，客户端分不清"结果少是因为库里就这么多"还是
    #: "结果少是因为服务在降级运行"—— 前者该改查询，后者该等或扩容，
    #: 处理方式完全相反。
    effective_top_k: int = 0
    #: 当前降级级别（NORMAL / NO_RERANK / …）。NORMAL 时为 None。
    degraded: str | None = None
    #: 实际执行的模式与召回通道。M3-T7 单路降级时二者可能不同于请求值。
    mode: SearchMode = SearchMode.DENSE
    channels: tuple[RecallChannel, ...] = (RecallChannel.DENSE,)
    #: 与系统负载级别 `degraded` 分开，避免客户端混淆两种完全不同的降级。
    degradations: tuple[RetrievalDegradation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks": [c.to_dict() for c in self.chunks],
            "reranked": self.reranked,
            "fetched": self.fetched,
            "effective_top_k": self.effective_top_k,
            "degraded": self.degraded,
            "mode": self.mode,
            "channels": list(self.channels),
            "degradations": [item.to_dict() for item in self.degradations],
        }


@dataclass(slots=True)
class _RecallResult:
    chunks: list[RetrievedChunk]
    mode: SearchMode
    channels: tuple[RecallChannel, ...]
    degradations: tuple[RetrievalDegradation, ...] = ()


class RetrievalService:
    def __init__(
        self,
        *,
        embedding_model: EmbeddingPort,
        vector_store: VectorSearchPort,
        keyword_search: KeywordSearchPort | None = None,
        knowledge_base: KnowledgeBaseService | None = None,
        reranker: RerankerPort | None = None,
        degradation: DegradationController | None = None,
    ) -> None:
        self._embedding_model = embedding_model
        self._vector_store = vector_store
        self._keyword_search = keyword_search
        self._knowledge_base = knowledge_base
        self._reranker = reranker
        #: 分级降级（S4-5）。None = 不降级（当库用、单测）。
        self._degradation = degradation

    async def search(self, query: SearchQuery) -> RetrievalResult:
        # 降级顺序的落点：L1 关 rerank（最贵、且没它检索仍可用），
        # L2 再砍 top_k。两级都只影响**质量**，不影响"能不能拿到结果"。
        top_k = query.top_k
        allow_rerank = True
        level: str | None = None
        if self._degradation is not None:
            current = self._degradation.level()
            level = current.name if current else None
            top_k = self._degradation.adjust_top_k(top_k)
            allow_rerank = self._degradation.allow_rerank()
        if level == "NORMAL":
            level = None

        recall = await self._recall(query)
        candidates = recall.chunks
        if not candidates:
            return RetrievalResult(
                chunks=[],
                reranked=False,
                fetched=0,
                effective_top_k=top_k,
                degraded=level,
                mode=recall.mode,
                channels=recall.channels,
                degradations=recall.degradations,
            )

        # 写成"先取出再判 None"而不是布尔标志：标志变量会把"reranker 不是
        # None"这个结论丢掉，到调用点就得再补一次断言。
        reranker = self._reranker if (query.rerank and allow_rerank) else None
        if reranker is None:
            return RetrievalResult(
                chunks=candidates[:top_k],
                reranked=False,
                fetched=len(candidates),
                effective_top_k=top_k,
                degraded=level,
                mode=recall.mode,
                channels=recall.channels,
                degradations=recall.degradations,
            )

        chunks, did_rerank, rerank_degradation = await self._rerank(
            reranker, query.query, candidates
        )
        degradations = recall.degradations
        if rerank_degradation is not None:
            degradations = (*degradations, rerank_degradation)
        return RetrievalResult(
            chunks=chunks[:top_k],
            reranked=did_rerank,
            fetched=len(candidates),
            effective_top_k=top_k,
            degraded=level,
            mode=recall.mode,
            channels=recall.channels,
            degradations=degradations,
        )

    async def _recall(self, query: SearchQuery) -> _RecallResult:
        # 与入库路径使用同一条 A12 模型守卫。同维度的不同模型不会触发向量库
        # 报错，却会在不兼容的语义空间中比较向量，结果只会静默变差。
        if self._knowledge_base is not None:
            await self._knowledge_base.resolve_for_search(query.kb_id)
        filter = _normalize_filter(query.filter)
        fetch_k = query.effective_fetch_k()

        if query.mode is SearchMode.DENSE:
            hits = await self._dense_hits(query, fetch_k=fetch_k, filter=filter)
            return _RecallResult(
                chunks=self._dense_chunks(hits),
                mode=SearchMode.DENSE,
                channels=(RecallChannel.DENSE,),
            )

        keyword_search = self._keyword_search
        if keyword_search is None:
            raise KeywordSearchUnavailable(
                f"mode={query.mode.value} 需要 KeywordSearchPort，"
                "请在组合根装配关键词检索实现"
            )

        if query.mode is SearchMode.KEYWORD:
            hits = await keyword_search.asearch_keywords(
                query.kb_id,
                query.query,
                top_k=fetch_k,
                filter=filter,
            )
            return _RecallResult(
                chunks=self._keyword_chunks(hits),
                mode=SearchMode.KEYWORD,
                channels=(RecallChannel.KEYWORD,),
            )

        return await self._hybrid_recall(
            query,
            keyword_search=keyword_search,
            fetch_k=fetch_k,
            filter=filter,
        )

    async def _hybrid_recall(
        self,
        query: SearchQuery,
        *,
        keyword_search: KeywordSearchPort,
        fetch_k: int,
        filter: Filter | None,
    ) -> _RecallResult:
        dense_result, keyword_result = await asyncio.gather(
            self._dense_hits(query, fetch_k=fetch_k, filter=filter),
            keyword_search.asearch_keywords(
                query.kb_id,
                query.query,
                top_k=fetch_k,
                filter=filter,
            ),
            return_exceptions=True,
        )

        for result in (dense_result, keyword_result):
            if isinstance(result, BaseException) and _must_propagate(result):
                raise result

        dense_error = dense_result if isinstance(dense_result, BaseException) else None
        keyword_error = (
            keyword_result if isinstance(keyword_result, BaseException) else None
        )
        if dense_error is not None and keyword_error is not None:
            failures = (
                _channel_degradation(RetrievalStage.DENSE, dense_error),
                _channel_degradation(RetrievalStage.KEYWORD, keyword_error),
            )
            _log_channel_failure(failures[0], dense_error)
            _log_channel_failure(failures[1], keyword_error)
            raise HybridRecallFailed(failures)

        if dense_error is not None:
            failure = _channel_degradation(RetrievalStage.DENSE, dense_error)
            _log_channel_failure(failure, dense_error)
            return _RecallResult(
                chunks=self._keyword_chunks(cast("list[SearchHit]", keyword_result)),
                mode=SearchMode.KEYWORD,
                channels=(RecallChannel.KEYWORD,),
                degradations=(failure,),
            )

        if keyword_error is not None:
            failure = _channel_degradation(RetrievalStage.KEYWORD, keyword_error)
            _log_channel_failure(failure, keyword_error)
            return _RecallResult(
                chunks=self._dense_chunks(cast("list[SearchHit]", dense_result)),
                mode=SearchMode.DENSE,
                channels=(RecallChannel.DENSE,),
                degradations=(failure,),
            )

        dense_hits = cast("list[SearchHit]", dense_result)
        keyword_hits = cast("list[SearchHit]", keyword_result)
        fused = reciprocal_rank_fusion(
            {
                RecallChannel.DENSE.value: dense_hits,
                RecallChannel.KEYWORD.value: keyword_hits,
            }
        )
        return _RecallResult(
            chunks=[self._fused_chunk(hit) for hit in fused],
            mode=SearchMode.HYBRID,
            channels=(RecallChannel.DENSE, RecallChannel.KEYWORD),
        )

    async def _dense_hits(
        self,
        query: SearchQuery,
        *,
        fetch_k: int,
        filter: Filter | None,
    ) -> list[SearchHit]:
        embedding = await self._embedding_model.aembed_query(query.query)
        return await self._vector_store.asearch(
            query.kb_id,
            embedding,
            top_k=fetch_k,
            filter=filter,
        )

    @staticmethod
    def _dense_chunks(hits: list[SearchHit]) -> list[RetrievedChunk]:
        return [
            RetrievedChunk(
                id=hit.id,
                text=hit.text,
                score=hit.score,
                metadata=hit.metadata,
                vector_score=hit.score,
                vector_rank=rank,
            )
            for rank, hit in enumerate(hits, start=1)
        ]

    @staticmethod
    def _keyword_chunks(hits: list[SearchHit]) -> list[RetrievedChunk]:
        return [
            RetrievedChunk(
                id=hit.id,
                text=hit.text,
                score=hit.score,
                metadata=hit.metadata,
                keyword_score=hit.score,
                keyword_rank=rank,
            )
            for rank, hit in enumerate(hits, start=1)
        ]

    @staticmethod
    def _fused_chunk(hit: FusedHit) -> RetrievedChunk:
        dense = hit.contributions.get(RecallChannel.DENSE.value)
        keyword = hit.contributions.get(RecallChannel.KEYWORD.value)
        return RetrievedChunk(
            id=hit.id,
            text=hit.text,
            score=hit.score,
            metadata=dict(hit.metadata),
            vector_score=dense.score if dense is not None else None,
            keyword_score=keyword.score if keyword is not None else None,
            fusion_score=hit.score,
            vector_rank=dense.rank if dense is not None else None,
            keyword_rank=keyword.rank if keyword is not None else None,
        )

    @staticmethod
    async def _rerank(
        reranker: RerankerPort, query: str, candidates: list[RetrievedChunk]
    ) -> tuple[list[RetrievedChunk], bool, RetrievalDegradation | None]:
        """重排并返回结果、执行状态及安全降级诊断。

        重排失败时**降级返回原始召回结果**，而不是让整个查询失败 ——
        检索是读路径，给出稍差的结果远好过给不出结果。降级必须留下日志，
        否则线上质量下滑无人察觉（spec S4-5）。

        显式返回布尔而非让调用方比对列表身份：后者能跑通但极易在重构中失效。
        reranker 由调用方传入而非从 self 取，省掉一个"此处它必不为 None"的断言。
        """
        try:
            ranked = await reranker.arank(
                query,
                [
                    RerankDocument(
                        id=candidate.id,
                        content=candidate.text,
                        metadata=candidate.metadata,
                    )
                    for candidate in candidates
                ],
            )
        except Exception as exc:  # noqa: BLE001 —— 任何重排故障都降级
            logger.warning(f"重排失败，降级为原始召回结果：{exc!r}")
            return (
                candidates,
                False,
                RetrievalDegradation(
                    stage=RetrievalStage.RERANKER,
                    reason="request_failed",
                    error_type=type(exc).__name__,
                ),
            )

        if len(ranked) != len(candidates):
            logger.warning(
                f"重排返回 {len(ranked)} 个结果但候选有 {len(candidates)} 个，"
                f"结果不可对齐，降级为原始召回结果"
            )
            return (
                candidates,
                False,
                RetrievalDegradation(
                    stage=RetrievalStage.RERANKER,
                    reason="result_count_mismatch",
                ),
            )

        indexes = [item.index for item in ranked]
        if sorted(indexes) != list(range(len(candidates))):
            logger.warning(
                f"重排结果索引 {indexes} 无法与 {len(candidates)} 个候选对齐，"
                "降级为原始召回结果"
            )
            return (
                candidates,
                False,
                RetrievalDegradation(
                    stage=RetrievalStage.RERANKER,
                    reason="result_index_mismatch",
                ),
            )

        rescored = [
            RetrievedChunk(
                id=candidates[item.index].id,
                text=candidates[item.index].text,
                score=item.score,
                metadata=candidates[item.index].metadata,
                vector_score=candidates[item.index].vector_score,
                keyword_score=candidates[item.index].keyword_score,
                fusion_score=candidates[item.index].fusion_score,
                vector_rank=candidates[item.index].vector_rank,
                keyword_rank=candidates[item.index].keyword_rank,
            )
            for item in ranked
        ]
        return rescored, True, None


_NON_DEGRADABLE_RECALL_ERRORS = (
    AssertionError,
    AttributeError,
    CollectionNotFound,
    CollectionSchemaMismatch,
    DimensionMismatch,
    KeyError,
    MemoryError,
    NotImplementedError,
    TypeError,
    ValueError,
)


def _must_propagate(exc: BaseException) -> bool:
    """请求、数据与编程错误不能伪装成一次可恢复的通道抖动。"""
    return not isinstance(exc, Exception) or isinstance(
        exc, _NON_DEGRADABLE_RECALL_ERRORS
    )


def _channel_degradation(
    stage: RetrievalStage, exc: BaseException
) -> RetrievalDegradation:
    return RetrievalDegradation(
        stage=stage,
        reason="channel_unavailable",
        error_type=type(exc).__name__,
    )


def _log_channel_failure(degradation: RetrievalDegradation, exc: BaseException) -> None:
    # API 只返回错误类型，完整异常链仅进入服务日志，避免把连接信息带给调用方。
    logger.opt(exception=exc).warning(
        f"hybrid 召回通道失败，降级继续 stage={degradation.stage.value} "
        f"error_type={degradation.error_type}"
    )


def _normalize_filter(filter: dict[str, Any] | None) -> Filter | None:
    """空 dict 视为无过滤，避免调用方传 `{}` 时被当成"匹配所有键"。"""
    return filter or None


__all__ = [
    "HybridRecallFailed",
    "KeywordSearchUnavailable",
    "RecallChannel",
    "RetrievalDegradation",
    "RetrievalResult",
    "RetrievalService",
    "RetrievalStage",
    "RetrievedChunk",
    "SearchQuery",
    "SearchMode",
]
