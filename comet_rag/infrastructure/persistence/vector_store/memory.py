"""用于测试与单进程模式的内存向量存储。"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any

from comet_rag.ports import (
    AsyncVectorStore,
    CollectionNotFound,
    DimensionMismatch,
    Filter,
    SearchHit,
    VectorRecord,
    matches_filter,
)
from comet_rag.ports.filtering import normalize_filter

_TERM = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_BM25_K1 = 1.2
_BM25_B = 0.75


def _tokenize(text: str) -> list[str]:
    """提供不依赖外部分词器的确定性参考行为，不模拟供应商 analyzer 细节。"""
    return [match.group(0).casefold() for match in _TERM.finditer(text)]


def _bm25_scores(
    records: Sequence[VectorRecord], query_terms: Sequence[str]
) -> dict[str, float]:
    documents = {record.id: _tokenize(record.text) for record in records}
    if not documents or not query_terms:
        return {}

    document_count = len(documents)
    average_length = sum(map(len, documents.values())) / document_count
    frequencies = {rid: Counter(tokens) for rid, tokens in documents.items()}
    document_frequency = {
        term: sum(term in frequency for frequency in frequencies.values())
        for term in set(query_terms)
    }

    scores: dict[str, float] = {}
    for rid, frequency in frequencies.items():
        length = len(documents[rid])
        score = 0.0
        for term in set(query_terms):
            term_frequency = frequency[term]
            if term_frequency == 0:
                continue
            inverse_frequency = math.log(
                1
                + (document_count - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            normalization = term_frequency + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * length / (average_length or 1)
            )
            score += inverse_frequency * term_frequency * (_BM25_K1 + 1) / normalization
        if score > 0:
            scores[rid] = score
    return scores


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度，值域 [-1, 1]。任一向量为零向量时返回 0。"""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class _Collection:
    __slots__ = ("dim", "records")

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.records: dict[str, VectorRecord] = {}


class AsyncInMemoryVectorStore(AsyncVectorStore):
    def __init__(self) -> None:
        self._collections: dict[str, _Collection] = {}
        self._lock = asyncio.Lock()

    def _require(self, kb_id: str) -> _Collection:
        try:
            return self._collections[kb_id]
        except KeyError:
            raise CollectionNotFound(kb_id) from None

    async def ensure_collection(self, kb_id: str, *, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"维度必须为正整数，收到 {dim}")
        async with self._lock:
            existing = self._collections.get(kb_id)
            if existing is None:
                self._collections[kb_id] = _Collection(dim)
                return
            if existing.dim != dim:
                raise DimensionMismatch(kb_id, existing.dim, dim)

    async def upsert(self, kb_id: str, records: Sequence[VectorRecord]) -> list[str]:
        async with self._lock:
            collection = self._require(kb_id)
            # 先整体校验再写入：宁可一条不写，也不要写一半留下不一致的库
            for record in records:
                if len(record.embedding) != collection.dim:
                    raise DimensionMismatch(
                        kb_id, collection.dim, len(record.embedding)
                    )
            # 复制也可能失败；整批准备成功后才替换库内数据。
            prepared = {
                record.id: VectorRecord(
                    id=record.id,
                    text=record.text,
                    embedding=list(record.embedding),
                    metadata=deepcopy(record.metadata),
                )
                for record in records
            }
            collection.records.update(prepared)
            return [r.id for r in records]

    async def search(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        filter = normalize_filter(filter)
        async with self._lock:
            collection = self._require(kb_id)
            if len(query_embedding) != collection.dim:
                raise DimensionMismatch(kb_id, collection.dim, len(query_embedding))
            candidates = [
                record
                for record in collection.records.values()
                if matches_filter(record.metadata, filter)
            ]

        scored = [
            SearchHit(
                id=r.id,
                text=r.text,
                score=cosine_similarity(query_embedding, r.embedding),
                metadata=r.metadata,
            )
            for r in candidates
        ]
        # id 作为次级键，保证同分时顺序稳定（否则快照/断言会随机失败）
        scored.sort(key=lambda h: (-h.score, h.id))
        # 库内 metadata 不再原地修改；只复制实际返回的命中，避免复制整库。
        return [replace(hit, metadata=deepcopy(hit.metadata)) for hit in scored[:top_k]]

    async def search_keywords(
        self,
        kb_id: str,
        query: str,
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        filter = normalize_filter(filter)
        if top_k <= 0:
            raise ValueError(f"top_k 必须为正整数，收到 {top_k}")
        async with self._lock:
            collection = self._require(kb_id)
            records = list(collection.records.values())
        query_terms = _tokenize(query)
        scores = _bm25_scores(records, query_terms)
        hits = [
            SearchHit(
                id=record.id,
                text=record.text,
                score=scores[record.id],
                metadata=record.metadata,
            )
            for record in records
            if record.id in scores and matches_filter(record.metadata, filter)
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.id))
        return [replace(hit, metadata=deepcopy(hit.metadata)) for hit in hits[:top_k]]

    async def delete(
        self,
        kb_id: str,
        *,
        ids: Sequence[str] | None = None,
        filter: Filter | None = None,
    ) -> int:
        filter = normalize_filter(filter)
        if ids is None and not filter:
            raise ValueError("删除必须提供 ids 或非空 Filter，不能隐式清空知识库")
        async with self._lock:
            collection = self._require(kb_id)
            targets = set(ids) if ids is not None else set(collection.records)
            doomed = [
                key
                for key in targets
                if key in collection.records
                and matches_filter(collection.records[key].metadata, filter)
            ]
            for key in doomed:
                del collection.records[key]
            return len(doomed)

    async def drop_collection(self, kb_id: str) -> None:
        async with self._lock:
            self._collections.pop(kb_id, None)

    async def count(self, kb_id: str, *, filter: Filter | None = None) -> int:
        filter = normalize_filter(filter)
        async with self._lock:
            collection = self._require(kb_id)
            return sum(
                1
                for r in collection.records.values()
                if matches_filter(r.metadata, filter)
            )

    # ── 仅供测试与调试 ─────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            kb_id: [
                {"id": r.id, "text": r.text, "metadata": deepcopy(r.metadata)}
                for r in collection.records.values()
            ]
            for kb_id, collection in self._collections.items()
        }
