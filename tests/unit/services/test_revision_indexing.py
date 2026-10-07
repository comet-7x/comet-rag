from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from hashlib import sha256

import pytest

from comet_rag.engines.chunkers import ChunkDraft, FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.vector_store import InMemoryVectorStore
from comet_rag.ports import Filter, NormalizedDocument, VectorRecord
from comet_rag.ports.document_store import (
    RevisionConflict,
    RevisionDraft,
    RevisionKey,
    RevisionNotReady,
    RevisionStatus,
    StoredParent,
    StoredRevision,
)
from comet_rag.services.indexing import IndexPlan, IndexPlanner, RevisionStager


class FakeEmbedding(BaseEmbeddingModel):
    def __init__(self) -> None:
        self.calls = 0
        self.failure = False

    def _embed(self, data, **kwargs) -> list[float]:
        return [1.0, float(len(str(data)))]

    async def _aembed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        if self.failure:
            raise ConnectionError("embedding interrupted")
        return self._embed(data)

    async def close_client(self) -> None:
        return None


class FaultyVectors(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.fail_on = 0
        self.count_delta = 0

    async def aupsert(self, kb_id: str, records: Sequence[VectorRecord]) -> list[str]:
        self.calls += 1
        result = await super().aupsert(kb_id, records)
        if self.calls == self.fail_on:
            raise ConnectionError("response lost after vector commit")
        return result

    async def acount(self, kb_id: str, *, filter: Filter | None = None) -> int:
        return await super().acount(kb_id, filter=filter) + self.count_delta


class FaultyDocuments(InMemoryDocumentStore):
    def __init__(self) -> None:
        super().__init__()
        self.parent_calls = 0
        self.fail_parent_on = 0
        self.fail_ready = False

    async def aput_parents(
        self, revision: RevisionKey, parents: Sequence[StoredParent]
    ) -> None:
        self.parent_calls += 1
        await super().aput_parents(revision, parents)
        if self.parent_calls == self.fail_parent_on:
            raise ConnectionError("response lost after parent commit")

    async def amark_ready(self, revision: RevisionKey) -> StoredRevision:
        result = await super().amark_ready(revision)
        if self.fail_ready:
            raise ConnectionError("response lost after ready commit")
        return result


def make_plan(
    revision: str = "r1", text: str = "abcdEFGHijkl", *, hierarchy: bool = True
) -> IndexPlan:
    document = NormalizedDocument(markdown=text)
    chunks = (
        HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(3)).split(document)
        if hierarchy
        else FixedSizeChunker(3).split(text)
    )
    return IndexPlanner().plan(
        chunks,
        key=RevisionKey("kb", "source", revision),
        source="sample.md",
        file_type="md",
    )


def stager(documents, vectors, embedding) -> RevisionStager:
    return RevisionStager(
        document_store=documents,
        vector_store=vectors,
        embedding_model=embedding,
        parent_batch_size=1,
        embed_window=2,
    )


def manifest(plan: IndexPlan) -> RevisionDraft:
    return RevisionDraft(
        plan.key,
        plan.plan_sha256,
        len(plan.parents),
        len(plan.vector_children),
        plan.source,
        plan.file_type,
        plan.metadata,
    )


def test_revision_ids_relations_and_digest() -> None:
    first, second = make_plan("r1"), make_plan("r2")
    assert first.plan_sha256 == second.plan_sha256
    assert first.vector_children[0].id == sha256(b"source:r1:child:0").hexdigest()
    assert first.parents[0].id == sha256(b"source:r1:parent:0").hexdigest()
    assert {c.id for c in first.vector_children}.isdisjoint(
        c.id for c in second.vector_children
    )
    assert first.vector_children[0].parent_id == first.parents[0].id
    assert first.vector_children[2].parent_id == first.parents[1].id
    assert first.vector_children[0].next_id == first.vector_children[1].id
    assert first.vector_children[-1].next_id is None
    assert first.plan_sha256 != make_plan(text="zzzzEFGHijkl").plan_sha256


def test_flat_legacy_hook_without_span_and_reserved_metadata() -> None:
    chunks = [
        ChunkDraft(
            "正文",
            0,
            metadata={
                "source_id": "forged",
                "parent_id": "forged",
                "previous_id": "forged",
            },
        )
    ]
    plan = IndexPlanner().plan(
        chunks,
        key=RevisionKey("kb", "source", "r1"),
        source="doc",
        file_type="md",
        request_metadata={"next_id": "forged", "owner": "request"},
    )
    assert plan.parents == ()
    child = plan.vector_children[0]
    assert child.start_char is None and child.parent_id is None
    assert child.metadata["source_id"] == "source"
    assert child.metadata["previous_id"] is None
    assert child.metadata["next_id"] is None
    assert child.metadata["owner"] == "request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["parents", "embedding", "vectors", "count_less", "count_more", "ready"]
)
async def test_faults_preserve_old_active_and_retry_same_revision(failure: str) -> None:
    documents, vectors, embedding = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    writer = stager(documents, vectors, embedding)
    old, new = make_plan("old"), make_plan("new", "ZZZZYYYYxxxx")
    await writer.astage(old, embedding_dim=2)
    await documents.aactivate_revision(old.key)
    old_hits = await vectors.asearch(
        "kb", [1, 1], top_k=20, filter={"document_revision": "old"}
    )
    if failure == "parents":
        documents.fail_parent_on = documents.parent_calls + 1
    elif failure == "embedding":
        embedding.failure = True
    elif failure == "vectors":
        vectors.fail_on = vectors.calls + 1
    elif failure == "count_less":
        vectors.count_delta = -1
    elif failure == "count_more":
        vectors.count_delta = 1
    else:
        documents.fail_ready = True
    with pytest.raises((ConnectionError, RevisionNotReady)):
        await writer.astage(new, embedding_dim=2)
    assert await documents.aget_active_revisions("kb", ["source"]) == {"source": "old"}
    unchanged = await vectors.asearch(
        "kb", [1, 1], top_k=20, filter={"document_revision": "old"}
    )
    assert [(h.id, h.text) for h in unchanged] == [(h.id, h.text) for h in old_hits]
    reserved = await documents.areserve_revision(manifest(new))
    documents.fail_parent_on = 0
    documents.fail_ready = False
    vectors.fail_on = vectors.count_delta = 0
    embedding.failure = False
    prepared = await writer.astage(new, embedding_dim=2)
    assert prepared.status is RevisionStatus.READY
    assert prepared.generation == reserved.generation == 2
    assert await vectors.acount("kb", filter={"document_revision": "new"}) == 4
    calls = embedding.calls
    await writer.astage(new, embedding_dim=2)
    assert embedding.calls == calls
    assert await documents.aget_active_revisions("kb", ["source"]) == {"source": "old"}


@pytest.mark.asyncio
async def test_same_revision_changed_plan_rejected_without_vector_write() -> None:
    documents, vectors, embedding = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    writer = stager(documents, vectors, embedding)
    await writer.astage(make_plan(), embedding_dim=2)
    calls = vectors.calls
    with pytest.raises(RevisionConflict):
        await writer.astage(make_plan(text="ZZZZYYYYxxxx"), embedding_dim=2)
    assert vectors.calls == calls


@pytest.mark.asyncio
async def test_staging_checkpoint_stops_before_vector_write() -> None:
    documents, vectors, embedding = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    count = 0

    async def checkpoint() -> None:
        nonlocal count
        count += 1
        # prepare 的 reserve/父批次之后，resume 的父块复验也必须经过检查点。
        if count == 5:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        await stager(documents, vectors, embedding).astage(
            make_plan(), embedding_dim=2, checkpoint=checkpoint
        )
    assert vectors.calls == 0
    assert (
        await documents.areserve_revision(manifest(make_plan()))
    ).status is RevisionStatus.STAGING


@pytest.mark.asyncio
@pytest.mark.parametrize("hierarchy", [True, False])
async def test_parent_not_embedded_and_first_revision_not_activated(
    hierarchy: bool,
) -> None:
    documents, vectors, embedding = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    plan = make_plan(hierarchy=hierarchy)
    await stager(documents, vectors, embedding).astage(plan, embedding_dim=2)
    assert embedding.calls == 4
    assert vectors.calls == 2
    assert await documents.aget_active_revisions("kb", ["source"]) == {"source": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "retired", "abandoned", "purged"])
async def test_closed_revision_not_rewritten(status: str) -> None:
    documents, vectors, embedding = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    writer = stager(documents, vectors, embedding)
    plan = make_plan()
    await writer.astage(plan, embedding_dim=2)
    if status in ("active", "retired"):
        await documents.aactivate_revision(plan.key)
        if status == "retired":
            newer = make_plan("r2")
            await writer.astage(newer, embedding_dim=2)
            await documents.aactivate_revision(newer.key)
    else:
        await documents.aabandon_revision(plan.key)
        if status == "purged":
            await documents.apurge_inactive_revision(plan.key)
    calls = vectors.calls
    with pytest.raises(RevisionConflict):
        await writer.astage(plan, embedding_dim=2)
    assert vectors.calls == calls


@pytest.mark.asyncio
async def test_mutated_plan_digest_and_ids_checked_before_reserve() -> None:
    documents, vectors, embedding = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    plan = make_plan()
    bad = replace(
        plan,
        vector_children=(
            replace(plan.vector_children[0], text="wrong"),
            *plan.vector_children[1:],
        ),
    )
    with pytest.raises(RevisionConflict, match="指纹"):
        await stager(documents, vectors, embedding).astage(bad, embedding_dim=2)
    assert (await documents.areserve_revision(manifest(plan))).generation == 1
    bad = replace(
        plan,
        vector_children=(
            replace(plan.vector_children[0], id="wrong"),
            *plan.vector_children[1:],
        ),
    )
    with pytest.raises(RevisionConflict, match="ID"):
        await stager(documents, vectors, embedding).astage(bad, embedding_dim=2)
    assert vectors.calls == 0
