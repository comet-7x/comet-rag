"""编排向量召回、可选重排与降级。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, Field, field_validator

from comet_rag.core.degradation import DegradationController
from comet_rag.core.logging import logger
from comet_rag.engines.retrieval import FusedHit, reciprocal_rank_fusion
from comet_rag.ports import (
    AsyncEmbeddingPort,
    AsyncKeywordSearchPort,
    AsyncRerankerPort,
    AsyncVectorSearchPort,
    CollectionNotFound,
    CollectionSchemaMismatch,
    DimensionMismatch,
    Filter,
    RerankDocument,
    SearchHit,
)
from comet_rag.ports.document_store import DocumentStore, StoredParent
from comet_rag.ports.filtering import normalize_filter
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.query_deadline import (
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    QueryDeadline,
    QueryDeadlineExceeded,
    validate_query_timeout,
)
from comet_rag.services.recall_execution import run_recall_channels
from comet_rag.services.reranking import InvalidRerankResult, validate_ranking
from comet_rag.services.revision_recall import (
    RevisionRecall,
    RevisionVisibilityError,
    VisibleRecall,
)


class SearchMode(StrEnum):
    DENSE = "dense"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class ReturnLevel(StrEnum):
    CHILD = "child"
    PARENT = "parent"


class RecallChannel(StrEnum):
    DENSE = "dense"
    KEYWORD = "keyword"


class RetrievalStage(StrEnum):
    DENSE = "dense"
    KEYWORD = "keyword"
    RERANKER = "reranker"
    PARENT = "parent"


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
    filter: Filter | None = None
    rerank: bool = Field(default=True, description="是否重排（未配置时自动跳过）")
    mode: SearchMode = Field(
        default=SearchMode.DENSE,
        description="召回模式；默认 dense 保持既有行为。",
    )
    return_level: ReturnLevel = ReturnLevel.CHILD

    @field_validator("filter", mode="before")
    @classmethod
    def validate_filter(cls, value: object) -> Filter | None:
        return None if value is None else normalize_filter(value)

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
    matched_child_id: str | None = None

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
            "matched_child_id": self.matched_child_id,
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
        embedding_model: AsyncEmbeddingPort,
        vector_store: AsyncVectorSearchPort,
        keyword_search: AsyncKeywordSearchPort | None = None,
        knowledge_base: KnowledgeBaseService | None = None,
        reranker: AsyncRerankerPort | None = None,
        degradation: DegradationController | None = None,
        document_store: DocumentStore | None = None,
        max_revision_candidates: int = 2048,
        timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        validate_query_timeout(timeout_seconds)
        self._timeout_seconds = timeout_seconds
        self._embedding_model = embedding_model
        self._vector_store = vector_store
        self._keyword_search = keyword_search
        self._knowledge_base = knowledge_base
        self._reranker = reranker
        #: 分级降级（S4-5）。None = 不降级（当库用、单测）。
        self._degradation = degradation
        self._document_store = document_store
        self._revision_recall = (
            RevisionRecall(document_store, max_candidates=max_revision_candidates)
            if document_store is not None
            else None
        )

    async def search(self, query: SearchQuery) -> RetrievalResult:
        deadline = QueryDeadline(self._timeout_seconds)
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

        recall = await self._recall(query, deadline)
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
        chunks, did_rerank, rerank_degradation = (
            await self._rerank(reranker, query.query, candidates, deadline)
            if reranker is not None
            else (candidates, False, None)
        )
        degradations = recall.degradations
        if rerank_degradation is not None:
            degradations = (*degradations, rerank_degradation)
        if query.return_level is ReturnLevel.PARENT:
            chunks, parent_degradation = await self._parents(
                query.kb_id, chunks, deadline
            )
            if parent_degradation is not None:
                degradations = (*degradations, parent_degradation)
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

    async def _recall(
        self, query: SearchQuery, deadline: QueryDeadline
    ) -> _RecallResult:
        # 与入库路径使用同一条 A12 模型守卫。同维度的不同模型不会触发向量库
        # 报错，却会在不兼容的语义空间中比较向量，结果只会静默变差。
        if self._knowledge_base is not None:
            knowledge_base = self._knowledge_base
            await deadline.run(
                lambda: knowledge_base.resolve_for_search(query.kb_id),
                stage="knowledge_base",
            )
        filter = _normalize_filter(query.filter)
        fetch_k = query.effective_fetch_k()

        if query.mode is SearchMode.DENSE:
            result = await deadline.run(
                lambda: self._dense_hits(query, fetch_k=fetch_k, filter=filter),
                stage="dense",
            )
            return _RecallResult(
                chunks=self._dense_chunks(result.hits),
                mode=SearchMode.DENSE,
                channels=(RecallChannel.DENSE,),
                degradations=_visibility_degradations(RetrievalStage.DENSE, result),
            )

        keyword_search = self._keyword_search
        if keyword_search is None:
            raise KeywordSearchUnavailable(
                f"mode={query.mode.value} 需要 AsyncKeywordSearchPort，"
                "请在组合根装配关键词检索实现"
            )

        if query.mode is SearchMode.KEYWORD:
            result = await deadline.run(
                lambda: self._keyword_hits(
                    query, keyword_search=keyword_search, fetch_k=fetch_k, filter=filter
                ),
                stage="keyword",
            )
            return _RecallResult(
                chunks=self._keyword_chunks(result.hits),
                mode=SearchMode.KEYWORD,
                channels=(RecallChannel.KEYWORD,),
                degradations=_visibility_degradations(RetrievalStage.KEYWORD, result),
            )

        return await self._hybrid_recall(
            query,
            deadline=deadline,
            keyword_search=keyword_search,
            fetch_k=fetch_k,
            filter=filter,
        )

    async def _hybrid_recall(
        self,
        query: SearchQuery,
        *,
        deadline: QueryDeadline,
        keyword_search: AsyncKeywordSearchPort,
        fetch_k: int,
        filter: Filter | None,
    ) -> _RecallResult:
        dense_result, keyword_result = await run_recall_channels(
            dense=lambda: deadline.run(
                lambda: self._dense_hits(query, fetch_k=fetch_k, filter=filter),
                stage="dense",
            ),
            keyword=lambda: deadline.run(
                lambda: self._keyword_hits(
                    query,
                    keyword_search=keyword_search,
                    fetch_k=fetch_k,
                    filter=filter,
                ),
                stage="keyword",
            ),
            must_propagate=_must_propagate,
        )

        dense_error = dense_result if isinstance(dense_result, BaseException) else None
        keyword_error = (
            keyword_result if isinstance(keyword_result, BaseException) else None
        )
        if dense_error is not None and keyword_error is not None:
            failures = (
                _channel_degradation(RetrievalStage.DENSE, dense_error),
                _channel_degradation(RetrievalStage.KEYWORD, keyword_error),
            )
            _log_channel_failure(failures[0], dense_error, continued=False)
            _log_channel_failure(failures[1], keyword_error, continued=False)
            for error in (dense_error, keyword_error):
                if isinstance(error, QueryDeadlineExceeded):
                    raise error
            raise HybridRecallFailed(failures)

        if dense_error is not None:
            failure = _channel_degradation(RetrievalStage.DENSE, dense_error)
            _log_channel_failure(failure, dense_error, continued=True)
            surviving = cast("VisibleRecall", keyword_result)
            return _RecallResult(
                chunks=self._keyword_chunks(surviving.hits),
                mode=SearchMode.KEYWORD,
                channels=(RecallChannel.KEYWORD,),
                degradations=(
                    failure,
                    *_visibility_degradations(RetrievalStage.KEYWORD, surviving),
                ),
            )

        if keyword_error is not None:
            failure = _channel_degradation(RetrievalStage.KEYWORD, keyword_error)
            _log_channel_failure(failure, keyword_error, continued=True)
            surviving = cast("VisibleRecall", dense_result)
            return _RecallResult(
                chunks=self._dense_chunks(surviving.hits),
                mode=SearchMode.DENSE,
                channels=(RecallChannel.DENSE,),
                degradations=(
                    failure,
                    *_visibility_degradations(RetrievalStage.DENSE, surviving),
                ),
            )

        dense_recall = cast("VisibleRecall", dense_result)
        keyword_recall = cast("VisibleRecall", keyword_result)
        fused = reciprocal_rank_fusion(
            {
                RecallChannel.DENSE.value: dense_recall.hits,
                RecallChannel.KEYWORD.value: keyword_recall.hits,
            }
        )
        return _RecallResult(
            chunks=[self._fused_chunk(hit) for hit in fused],
            mode=SearchMode.HYBRID,
            channels=(RecallChannel.DENSE, RecallChannel.KEYWORD),
            degradations=(
                *_visibility_degradations(RetrievalStage.DENSE, dense_recall),
                *_visibility_degradations(RetrievalStage.KEYWORD, keyword_recall),
            ),
        )

    async def _dense_hits(
        self,
        query: SearchQuery,
        *,
        fetch_k: int,
        filter: Filter | None,
    ) -> VisibleRecall:
        embedding = await self._embedding_model.embed_query(query.query)
        return await self._visible_hits(
            query.kb_id,
            fetch_k=fetch_k,
            recall=lambda limit: self._vector_store.search(
                query.kb_id,
                embedding,
                top_k=limit,
                filter=filter,
            ),
        )

    async def _keyword_hits(
        self,
        query: SearchQuery,
        *,
        keyword_search: AsyncKeywordSearchPort,
        fetch_k: int,
        filter: Filter | None,
    ) -> VisibleRecall:
        return await self._visible_hits(
            query.kb_id,
            fetch_k=fetch_k,
            recall=lambda limit: keyword_search.search_keywords(
                query.kb_id,
                query.query,
                top_k=limit,
                filter=filter,
            ),
        )

    async def _visible_hits(
        self,
        kb_id: str,
        *,
        fetch_k: int,
        recall: Callable[[int], Awaitable[list[SearchHit]]],
    ) -> VisibleRecall:
        if self._revision_recall is not None:
            return await self._revision_recall.recall(
                kb_id, fetch_k=fetch_k, recall=recall
            )
        hits = await recall(fetch_k)
        if any("document_revision" in hit.metadata for hit in hits):
            raise RevisionVisibilityError("revision recall requires DocumentStore")
        return VisibleRecall(hits)

    async def _parents(
        self,
        kb_id: str,
        candidates: list[RetrievedChunk],
        deadline: QueryDeadline,
    ) -> tuple[list[RetrievedChunk], RetrievalDegradation | None]:
        parent_ids: dict[str, None] = {}
        for child in candidates:
            parent_id = child.metadata.get("parent_id")
            if isinstance(parent_id, str) and parent_id:
                parent_ids[parent_id] = None
        if not parent_ids:
            return candidates, None
        try:
            if self._document_store is None:
                raise RevisionVisibilityError("parent lookup requires DocumentStore")
            document_store = self._document_store
            parents = await deadline.run(
                lambda: document_store.get_parents(kb_id, list(parent_ids)),
                stage="parent",
            )
        except Exception as exc:  # noqa: BLE001 — 正文回填失败可退回已判活 child
            logger.opt(exception=exc).warning("父块回填失败，退回 child")
            return candidates, RetrievalDegradation(
                RetrievalStage.PARENT,
                "deadline_exceeded"
                if isinstance(exc, QueryDeadlineExceeded)
                else "request_failed",
                type(exc).__name__,
            )
        result, damaged = _expand_parents(kb_id, candidates, parents)
        if damaged:
            logger.warning("父块缺失或 revision/KB 不匹配，相关项退回 child")
        return result, (
            RetrievalDegradation(RetrievalStage.PARENT, "invalid_parent")
            if damaged
            else None
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
        reranker: AsyncRerankerPort,
        query: str,
        candidates: list[RetrievedChunk],
        deadline: QueryDeadline,
    ) -> tuple[list[RetrievedChunk], bool, RetrievalDegradation | None]:
        """重排故障回退原候选；响应校验与物化同属降级边界，取消仍传播。"""
        try:
            documents = [
                RerankDocument(
                    id=candidate.id,
                    content=candidate.text,
                    # 失败后仍要返回原始候选，不能把其可变元数据借给适配器。
                    metadata=deepcopy(candidate.metadata),
                )
                for candidate in candidates
            ]
            result = await deadline.run(
                lambda: reranker.rank(query, documents),
                stage="reranker",
            )
            ranked = validate_ranking(result, candidate_count=len(candidates))
            rescored = [
                replace(candidates[item.index], score=item.score) for item in ranked
            ]
        except QueryDeadlineExceeded as exc:
            reason, error_type = "deadline_exceeded", type(exc).__name__
            logger.warning("重排预算耗尽，降级为原始召回结果")
        except InvalidRerankResult as exc:
            reason, error_type = exc.reason, None
            logger.warning("重排返回值无效，降级为原始召回结果 reason={}", reason)
        except Exception as exc:  # noqa: BLE001 —— 返回值处理失败也属于重排故障
            reason, error_type = "request_failed", type(exc).__name__
            logger.warning("重排失败，降级为原始召回结果：{!r}", exc)
        else:
            return rescored, True, None
        return (
            candidates,
            False,
            RetrievalDegradation(
                stage=RetrievalStage.RERANKER,
                reason=reason,
                error_type=error_type,
            ),
        )


def _expand_parents(
    kb_id: str,
    candidates: list[RetrievedChunk],
    parents: Mapping[str, StoredParent],
) -> tuple[list[RetrievedChunk], bool]:
    """只合并成功回填的父块；身份错误或缺失时保留每个已判活 child。"""
    result: list[RetrievedChunk] = []
    emitted: set[str] = set()
    damaged = False
    for child in candidates:
        parent_id = child.metadata.get("parent_id")
        if not isinstance(parent_id, str) or not parent_id:
            result.append(child)
            continue
        parent = parents.get(parent_id)
        if parent is None or (
            parent.parent_id != parent_id
            or parent.key.kb_id != kb_id
            or parent.key.source_id != child.metadata.get("source_id")
            or parent.key.revision_id != child.metadata.get("document_revision")
        ):
            damaged = True
            result.append(child)
            continue
        # 必须先核对身份：错误引用既不能抢占正确父块，也不能被同 ID 的父块吞掉。
        if parent_id in emitted:
            continue
        emitted.add(parent_id)
        result.append(_parent_chunk(child, parent))
    return result, damaged


def _parent_chunk(child: RetrievedChunk, parent: StoredParent) -> RetrievedChunk:
    # 位置与相邻关系属于 parent；所有检索/重排分数仍属于命中 child。
    metadata = {
        **child.metadata,
        **parent.metadata,
        "kb_id": parent.key.kb_id,
        "source_id": parent.key.source_id,
        "document_revision": parent.key.revision_id,
        "parent_id": parent.parent_id,
        "chunk_start": parent.start_char,
        "chunk_end": parent.end_char,
        "chunk_index": parent.ordinal,
        "previous_id": parent.previous_id,
        "next_id": parent.next_id,
    }
    # child 总数不是 parent 总数；Port 不提供 manifest 时不要伪造它。
    metadata.pop("total_chunks", None)
    return replace(
        child,
        id=parent.parent_id,
        text=parent.text,
        metadata=metadata,
        matched_child_id=child.id,
    )


_NON_DEGRADABLE_RECALL_ERRORS = (
    RevisionVisibilityError,
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


def _visibility_degradations(
    stage: RetrievalStage,
    result: VisibleRecall,
) -> tuple[RetrievalDegradation, ...]:
    return tuple(RetrievalDegradation(stage, reason) for reason in result.diagnostics)


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
        reason="deadline_exceeded"
        if isinstance(exc, QueryDeadlineExceeded)
        else "channel_unavailable",
        error_type=type(exc).__name__,
    )


def _log_channel_failure(
    degradation: RetrievalDegradation, exc: BaseException, *, continued: bool
) -> None:
    # API 只返回错误类型，完整异常链仅进入服务日志，避免把连接信息带给调用方。
    outcome = "降级继续" if continued else "请求失败"
    logger.opt(exception=exc).warning(
        f"hybrid 召回通道失败，{outcome} stage={degradation.stage.value} "
        f"error_type={degradation.error_type}"
    )


def _normalize_filter(filter: Filter | None) -> Filter | None:
    """复核构造后的赋值并把空映射归一化为无过滤。"""
    return normalize_filter(filter) or None


__all__ = [
    "HybridRecallFailed",
    "KeywordSearchUnavailable",
    "RecallChannel",
    "RetrievalDegradation",
    "RetrievalResult",
    "RetrievalService",
    "RetrievalStage",
    "RetrievedChunk",
    "ReturnLevel",
    "SearchQuery",
    "SearchMode",
]
