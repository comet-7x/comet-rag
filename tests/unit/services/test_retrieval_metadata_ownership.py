from __future__ import annotations

from typing import Any

import pytest

from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import VectorRecord
from comet_rag.ports.document_store import RevisionDraft, RevisionKey, StoredParent
from comet_rag.services.retrieval import (
    RetrievalService,
    ReturnLevel,
    SearchMode,
    SearchQuery,
)

KB = "kb-ownership"
SOURCE = "source"


class FixedEmbedding(AsyncBaseEmbeddingModel):
    # Any 与模型模板的供应商扩展参数保持一致；测试不使用这些参数。
    async def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        return [1.0, 0.0]


def reader(
    vectors: AsyncInMemoryVectorStore, documents: InMemoryDocumentStore
) -> RetrievalService:
    return RetrievalService(
        embedding_model=FixedEmbedding(),
        vector_store=vectors,
        keyword_search=vectors,
        document_store=documents,
    )


def query(mode: SearchMode, *, level: ReturnLevel = ReturnLevel.CHILD) -> SearchQuery:
    return SearchQuery(
        kb_id=KB,
        query="ownership",
        mode=mode,
        return_level=level,
        top_k=2,
        rerank=False,
    )


@pytest.fixture
async def child_reader() -> RetrievalService:
    vectors = AsyncInMemoryVectorStore()
    await vectors.ensure_collection(KB, dim=2)
    shared = {"labels": ["original"]}
    await vectors.upsert(
        KB,
        [
            VectorRecord(
                id=rid,
                text=f"ownership {rid}",
                embedding=[1.0, 0.0],
                metadata={"source_id": SOURCE, "nested": shared},
            )
            for rid in ("first", "second")
        ],
    )
    return reader(vectors, InMemoryDocumentStore())


@pytest.mark.parametrize("first_mode", list(SearchMode))
@pytest.mark.parametrize("next_mode", list(SearchMode))
async def test_editing_child_result_does_not_change_later_queries(
    child_reader: RetrievalService,
    first_mode: SearchMode,
    next_mode: SearchMode,
) -> None:
    first = await child_reader.search(query(first_mode))
    assert {chunk.id for chunk in first.chunks} == {"first", "second"}
    first.chunks[0].metadata["nested"]["labels"].append("caller edit")

    later = await child_reader.search(query(next_mode))

    assert {chunk.id for chunk in later.chunks} == {"first", "second"}
    assert [chunk.metadata["nested"] for chunk in later.chunks] == [
        {"labels": ["original"]},
        {"labels": ["original"]},
    ]


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_editing_one_child_does_not_change_sibling_result(
    child_reader: RetrievalService, mode: SearchMode
) -> None:
    result = await child_reader.search(query(mode))
    assert {chunk.id for chunk in result.chunks} == {"first", "second"}

    result.chunks[0].metadata["nested"]["labels"].append("caller edit")

    assert result.chunks[1].metadata["nested"] == {"labels": ["original"]}


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_editing_parent_result_does_not_change_stored_parent(
    mode: SearchMode,
) -> None:
    documents = InMemoryDocumentStore()
    revision = RevisionKey(KB, SOURCE, "revision")
    await documents.reserve_revision(
        RevisionDraft(
            key=revision,
            plan_sha256="a" * 64,
            parent_count=1,
            child_count=1,
            source="document.docx",
            file_type="docx",
        )
    )
    await documents.put_parents(
        revision,
        [
            StoredParent(
                key=revision,
                parent_id="parent",
                ordinal=0,
                text="ownership parent context",
                start_char=0,
                end_char=24,
                metadata={"nested": {"labels": ["parent original"]}},
            )
        ],
    )
    await documents.mark_ready(revision)
    await documents.activate_revision(revision)
    vectors = AsyncInMemoryVectorStore()
    await vectors.ensure_collection(KB, dim=2)
    await vectors.upsert(
        KB,
        [
            VectorRecord(
                id="child",
                text="ownership child",
                embedding=[1.0, 0.0],
                metadata={
                    "source_id": SOURCE,
                    "document_revision": revision.revision_id,
                    "parent_id": "parent",
                },
            )
        ],
    )
    service = reader(vectors, documents)
    first = await service.search(query(mode, level=ReturnLevel.PARENT))
    assert [chunk.id for chunk in first.chunks] == ["parent"]
    assert first.chunks[0].matched_child_id == "child"
    first.chunks[0].metadata["nested"]["labels"].append("caller edit")

    later = await service.search(query(mode, level=ReturnLevel.PARENT))

    assert [chunk.id for chunk in later.chunks] == ["parent"]
    assert later.chunks[0].matched_child_id == "child"
    assert later.chunks[0].metadata["nested"] == {"labels": ["parent original"]}
