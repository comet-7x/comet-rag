from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from types import MappingProxyType

from comet_rag.engines.chunkers import ChunkDraft
from comet_rag.engines.defaults import DEFAULT_EMBED_FANOUT, DEFAULT_EMBED_WINDOW
from comet_rag.engines.embedding.batch import aembed_documents
from comet_rag.engines.indexing import ChunkHierarchy
from comet_rag.ports import BaseVectorStore, EmbeddingPort, VectorRecord
from comet_rag.ports.document_store import (
    DocumentStore,
    RevisionConflict,
    RevisionDraft,
    RevisionKey,
    RevisionNotReady,
    RevisionStatus,
    StoredParent,
    StoredRevision,
)
from comet_rag.services.chunking import (
    _merge_chunk_metadata,
    dump_chunk_drafts,
    materialize_chunk_drafts,
)

# 限制单次 PostgreSQL 父块事务的正文量；服务装配时可按文档预算覆盖。
DEFAULT_PARENT_BATCH_SIZE = 128


def _freeze(metadata: Mapping[str, object]) -> Mapping[str, object]:
    # JSON 快照隔离调用者的嵌套对象，同时拒绝 NaN/非 JSON 值，指纹才能可重放。
    copied = json.loads(json.dumps(dict(metadata), ensure_ascii=False, allow_nan=False))
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class PlannedParent:
    id: str
    ordinal: int
    text: str
    start_char: int
    end_char: int
    previous_id: str | None
    next_id: str | None
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class PlannedChild:
    id: str
    ordinal: int
    parent_id: str | None
    text: str
    start_char: int | None
    end_char: int | None
    previous_id: str | None
    next_id: str | None
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class IndexPlan:
    key: RevisionKey
    plan_sha256: str
    source: str
    file_type: str
    metadata: Mapping[str, object]
    parents: tuple[PlannedParent, ...]
    vector_children: tuple[PlannedChild, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "parents", tuple(self.parents))
        object.__setattr__(self, "vector_children", tuple(self.vector_children))


def revision_chunk_id(key: RevisionKey, kind: str, ordinal: int) -> str:
    """revision 内确定性 ID；不得原地覆盖其他 revision 的块。"""
    if kind not in ("parent", "child") or type(ordinal) is not int or ordinal < 0:
        raise ValueError("kind 必须是 parent/child，ordinal 必须是非负整数")
    return sha256(
        f"{key.source_id}:{key.revision_id}:{kind}:{ordinal}".encode()
    ).hexdigest()


def _adjacent(ids: Sequence[str], ordinal: int) -> tuple[str | None, str | None]:
    return (
        ids[ordinal - 1] if ordinal else None,
        ids[ordinal + 1] if ordinal + 1 < len(ids) else None,
    )


def _digest(
    key: RevisionKey,
    source: str,
    file_type: str,
    metadata: Mapping[str, object],
    parents: Sequence[PlannedParent],
    children: Sequence[PlannedChild],
) -> str:
    parent_ordinals = {parent.id: parent.ordinal for parent in parents}
    canonical = {
        "kb_id": key.kb_id,
        "source_id": key.source_id,
        "source": source,
        "file_type": file_type,
        "metadata": dict(metadata),
        "parents": [
            {
                "ordinal": p.ordinal,
                "text": p.text,
                "start_char": p.start_char,
                "end_char": p.end_char,
                "metadata": dict(p.metadata),
            }
            for p in parents
        ],
        "children": [
            {
                "ordinal": c.ordinal,
                "text": c.text,
                "start_char": c.start_char,
                "end_char": c.end_char,
                "parent_ordinal": parent_ordinals[c.parent_id] if c.parent_id else None,
                "metadata": {
                    k: v
                    for k, v in c.metadata.items()
                    if k
                    not in ("document_revision", "parent_id", "previous_id", "next_id")
                },
            }
            for c in children
        ],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return sha256(encoded.encode()).hexdigest()


class IndexPlanner:
    """平坦/层级结果使用同一物化规则，revision ID 由调用方生成一次并持久化。"""

    def plan(
        self,
        chunks: Sequence[ChunkDraft] | ChunkHierarchy,
        /,
        *,
        key: RevisionKey,
        source: str,
        file_type: str,
        document_metadata: Mapping[str, object] | None = None,
        request_metadata: Mapping[str, object] | None = None,
    ) -> IndexPlan:
        hierarchy = chunks if isinstance(chunks, ChunkHierarchy) else None
        if isinstance(chunks, ChunkHierarchy):
            drafts = [
                ChunkDraft(
                    child.text,
                    child.ordinal,
                    child.start_char,
                    child.end_char,
                    child.metadata,
                )
                for child in chunks.children
            ]
        else:
            drafts = list(chunks)
        dump_chunk_drafts(drafts)
        materialized = materialize_chunk_drafts(
            drafts,
            source_id=key.source_id,
            source=source,
            file_type=file_type,
            kb_id=key.kb_id,
            document_metadata=document_metadata,
            request_metadata=request_metadata,
        )
        pure_parents = hierarchy.parents if hierarchy is not None else ()
        parent_ids = [revision_chunk_id(key, "parent", p.ordinal) for p in pure_parents]
        child_ids = [revision_chunk_id(key, "child", d.ordinal) for d in drafts]
        parents: list[PlannedParent] = []
        for parent in pure_parents:
            previous, following = _adjacent(parent_ids, parent.ordinal)
            parents.append(
                PlannedParent(
                    parent_ids[parent.ordinal],
                    parent.ordinal,
                    parent.text,
                    parent.start_char,
                    parent.end_char,
                    previous,
                    following,
                    _merge_chunk_metadata(
                        document=document_metadata,
                        request=request_metadata,
                        chunk=parent.metadata,
                    ),
                )
            )
        children: list[PlannedChild] = []
        for draft, chunk in zip(drafts, materialized, strict=True):
            previous, following = _adjacent(child_ids, draft.ordinal)
            parent_id = (
                parent_ids[hierarchy.children[draft.ordinal].parent_ordinal]
                if hierarchy is not None
                else None
            )
            metadata = {
                **chunk.metadata,
                "document_revision": key.revision_id,
                "parent_id": parent_id,
                "previous_id": previous,
                "next_id": following,
            }
            children.append(
                PlannedChild(
                    child_ids[draft.ordinal],
                    draft.ordinal,
                    parent_id,
                    draft.text,
                    draft.start_char,
                    draft.end_char,
                    previous,
                    following,
                    metadata,
                )
            )

        # 关系使用 ordinal 而非 revision ID；换 revision 不改内容指纹，换内容必须改。
        metadata = _merge_chunk_metadata(
            document=document_metadata, request=request_metadata
        )
        digest = _digest(key, source, file_type, metadata, parents, children)
        plan = IndexPlan(
            key,
            digest,
            source,
            file_type,
            metadata,
            tuple(parents),
            tuple(children),
        )
        # 在任何外部写入之前执行 manifest 的字段校验。
        RevisionDraft(
            key, digest, len(parents), len(children), source, file_type, plan.metadata
        )
        return plan


def _snapshot_plan(plan: IndexPlan) -> IndexPlan:
    snapshot = replace(
        plan,
        parents=tuple(replace(p) for p in plan.parents),
        vector_children=tuple(replace(c) for c in plan.vector_children),
    )
    for kind, chunks in (
        ("parent", snapshot.parents),
        ("child", snapshot.vector_children),
    ):
        ids = [c.id for c in chunks]
        for ordinal, chunk in enumerate(chunks):
            if chunk.ordinal != ordinal or chunk.id != revision_chunk_id(
                plan.key, kind, ordinal
            ):
                raise RevisionConflict(plan.key, "计划 ID 或 ordinal 不合法")
            if (chunk.previous_id, chunk.next_id) != _adjacent(ids, ordinal):
                raise RevisionConflict(plan.key, "计划相邻关系不合法")
    for child in snapshot.vector_children:
        required = {
            "document_revision": plan.key.revision_id,
            "source_id": plan.key.source_id,
            "kb_id": plan.key.kb_id,
            "parent_id": child.parent_id,
            "previous_id": child.previous_id,
            "next_id": child.next_id,
        }
        if any(child.metadata.get(k) != v for k, v in required.items()):
            raise RevisionConflict(plan.key, "子块系统字段不合法")
    if snapshot.plan_sha256 != _digest(
        snapshot.key,
        snapshot.source,
        snapshot.file_type,
        snapshot.metadata,
        snapshot.parents,
        snapshot.vector_children,
    ):
        raise RevisionConflict(plan.key, "计划内容与指纹不一致")
    return snapshot


class RevisionStager:
    """只写到 ready，不激活；生产启用必须先验收 revision-aware 读路径。"""

    def __init__(
        self,
        *,
        document_store: DocumentStore,
        vector_store: BaseVectorStore,
        embedding_model: EmbeddingPort,
        parent_batch_size: int = DEFAULT_PARENT_BATCH_SIZE,
        embed_window: int = DEFAULT_EMBED_WINDOW,
        max_concurrency: int = DEFAULT_EMBED_FANOUT,
    ) -> None:
        for value in (parent_batch_size, embed_window, max_concurrency):
            if type(value) is not int or value <= 0:
                raise ValueError("批量大小和并发必须为正整数")
        self._documents = document_store
        self._vectors = vector_store
        self._embedding = embedding_model
        self._parent_batch = parent_batch_size
        self._window = embed_window
        self._concurrency = max_concurrency

    async def astage(
        self,
        plan: IndexPlan,
        /,
        *,
        embedding_dim: int,
        task_id: str | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> StoredRevision:
        """失败保留本 revision 的 staging 供幂等重放，绝不覆盖或清理旧 active。"""

        if type(embedding_dim) is not int or embedding_dim <= 0:
            raise ValueError("embedding_dim 必须是正整数")
        plan = _snapshot_plan(plan)

        async def check() -> None:
            if checkpoint is not None:
                await checkpoint()

        await check()
        manifest = await self._documents.areserve_revision(
            RevisionDraft(
                plan.key,
                plan.plan_sha256,
                len(plan.parents),
                len(plan.vector_children),
                plan.source,
                plan.file_type,
                plan.metadata,
                task_id,
            )
        )
        if manifest.status not in (RevisionStatus.STAGING, RevisionStatus.READY):
            raise RevisionConflict(plan.key, f"不能准备 {manifest.status} revision")
        await self._vectors.aensure_collection(plan.key.kb_id, dim=embedding_dim)
        if manifest.status is RevisionStatus.STAGING:
            for start in range(0, len(plan.parents), self._parent_batch):
                await check()
                batch = [
                    StoredParent(
                        plan.key,
                        p.id,
                        p.ordinal,
                        p.text,
                        p.start_char,
                        p.end_char,
                        p.previous_id,
                        p.next_id,
                        p.metadata,
                    )
                    for p in plan.parents[start : start + self._parent_batch]
                ]
                await self._documents.aput_parents(plan.key, batch)
            for start in range(0, len(plan.vector_children), self._window):
                await check()
                batch = plan.vector_children[start : start + self._window]
                vectors = await aembed_documents(
                    self._embedding,
                    [c.text for c in batch],
                    max_concurrency=self._concurrency,
                )
                await check()
                await self._vectors.aupsert(
                    plan.key.kb_id,
                    [
                        VectorRecord(c.id, c.text, vector, dict(c.metadata))
                        for c, vector in zip(batch, vectors, strict=True)
                    ],
                )
        await check()
        actual = await self._vectors.acount(
            plan.key.kb_id,
            filter={
                "source_id": plan.key.source_id,
                "document_revision": plan.key.revision_id,
            },
        )
        if actual != len(plan.vector_children):
            raise RevisionNotReady(
                plan.key, f"子向量计数 {actual} != {len(plan.vector_children)}"
            )
        await check()
        return await self._documents.amark_ready(plan.key)
