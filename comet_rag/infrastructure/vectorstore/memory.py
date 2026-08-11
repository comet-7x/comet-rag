"""进程内向量存储。

用途有二，缺一不可：
  1. **测试**：unit 层不允许依赖 docker（spec §6），没有它整条链路就没法在
     单元测试里跑通
  2. **验证抽象**：只有第二个实现存在，才知道 `BaseVectorStore` 有没有被
     Milvus 的概念绑死。契约测试同时跑内存版与 Milvus 版，任何渗出接口的
     后端专有语义都会当场暴露（spec A9）

刻意**不用 numpy**：numpy 目前只是 `pdftext`（MinerU 依赖）带进来的传递依赖，
没有声明。在这里用它，等于让不装 mineru extra 的用户直接崩 —— 与 T2 修掉的
pyyaml 漏声明是同一类问题。纯 Python 对几千条向量足够快。

**不是生产用的**：全量线性扫描，没有索引、没有持久化，进程一停即丢。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Any

from comet_rag.infrastructure.vectorstore.base import (
    BaseVectorStore,
    CollectionNotFound,
    DimensionMismatch,
    Filter,
    SearchHit,
    VectorRecord,
    matches_filter,
)


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


class InMemoryVectorStore(BaseVectorStore):
    def __init__(self) -> None:
        self._collections: dict[str, _Collection] = {}
        self._lock = asyncio.Lock()

    def _require(self, kb_id: str) -> _Collection:
        try:
            return self._collections[kb_id]
        except KeyError:
            raise CollectionNotFound(kb_id) from None

    async def aensure_collection(self, kb_id: str, *, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"维度必须为正整数，收到 {dim}")
        async with self._lock:
            existing = self._collections.get(kb_id)
            if existing is None:
                self._collections[kb_id] = _Collection(dim)
                return
            if existing.dim != dim:
                raise DimensionMismatch(kb_id, existing.dim, dim)

    async def aupsert(self, kb_id: str, records: Sequence[VectorRecord]) -> list[str]:
        async with self._lock:
            collection = self._require(kb_id)
            # 先整体校验再写入：宁可一条不写，也不要写一半留下不一致的库
            for record in records:
                if len(record.embedding) != collection.dim:
                    raise DimensionMismatch(
                        kb_id, collection.dim, len(record.embedding)
                    )
            for record in records:
                collection.records[record.id] = VectorRecord(
                    id=record.id,
                    text=record.text,
                    embedding=list(record.embedding),
                    metadata=dict(record.metadata),
                )
            return [r.id for r in records]

    async def asearch(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
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
                metadata=dict(r.metadata),
            )
            for r in candidates
        ]
        # id 作为次级键，保证同分时顺序稳定（否则快照/断言会随机失败）
        scored.sort(key=lambda h: (-h.score, h.id))
        return scored[:top_k]

    async def adelete(
        self,
        kb_id: str,
        *,
        ids: Sequence[str] | None = None,
        filter: Filter | None = None,
    ) -> int:
        if ids is None and filter is None:
            raise ValueError("ids 与 filter 至少给一个，否则等于清空整个知识库")
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

    async def adrop_collection(self, kb_id: str) -> None:
        async with self._lock:
            self._collections.pop(kb_id, None)

    async def acount(self, kb_id: str, *, filter: Filter | None = None) -> int:
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
                {"id": r.id, "text": r.text, "metadata": dict(r.metadata)}
                for r in collection.records.values()
            ]
            for kb_id, collection in self._collections.items()
        }
