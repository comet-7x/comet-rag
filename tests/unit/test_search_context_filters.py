from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from comet_rag.chunkers import SourceRef
from comet_rag.docstores.base import AsyncDocStore
from comet_rag.docstores.memory import AsyncMemoryDocStore
from comet_rag.docstores.types import WriteManifest, content_digest
from comet_rag.embedder.types import EmbeddingSpace
from comet_rag.indexing.types import IndexUnit, Representation
from comet_rag.pipelines import AsyncSearchPipeline
from comet_rag.vectorstores.filters import And, Eq, Filter, In
from comet_rag.vectorstores.memory import AsyncMemoryVectorStore
from comet_rag.vectorstores.types import VectorRecord


async def search_fixture(
    *, parent_visibility: str = "restricted", docstore: AsyncDocStore | None = None
):
    index_scope = docstore.index_scope if docstore is not None else "scope"
    space = EmbeddingSpace("test-only", 2, "unit-test")
    parent = IndexUnit(
        index_scope,
        "profile",
        "source",
        "document",
        "cat public passage; RESTRICTED parent content",
        (SourceRef("document", "parent"),),
        metadata={"visibility": parent_visibility},
    )
    child = IndexUnit(
        index_scope,
        "profile",
        "source",
        "document",
        "cat public passage",
        (SourceRef("document", "child"),),
        parent_unit_id=parent.unit_id,
        metadata={"visibility": "public"},
    )
    rep = Representation(
        child.unit_id,
        index_scope,
        "profile",
        "source",
        "document",
        child.content,
    )
    record = VectorRecord(
        rep.representation_id,
        child.unit_id,
        "source",
        "document",
        index_scope,
        "profile",
        space.space_id,
        (1.0, 0.0),
        "cat public passage",
        metadata=child.metadata,
    )
    manifest = WriteManifest(
        index_scope,
        "profile",
        "source",
        "document",
        space.space_id,
        (parent.unit_id, child.unit_id),
        (rep.representation_id,),
        {record.record_id: record.fingerprint},
        content_digest((parent, child), (rep,)),
        {record.record_id: record.vector},
    )
    docs = (
        docstore
        if docstore is not None
        else AsyncMemoryDocStore(index_scope=index_scope)
    )
    vectors = AsyncMemoryVectorStore(
        index_scope=index_scope,
        index_profile_id="profile",
        space=space,
        field_types={"visibility": str},
    )
    await docs.upsert_units((parent, child), representations=(rep,), manifest=manifest)
    await vectors.upsert((record,))
    return AsyncSearchPipeline(docstore=docs, vectorstore=vectors), parent, child


@pytest.mark.parametrize("budget", [None, 0])
@pytest.mark.parametrize("filter_kind", ["eq", "in", "and", "unit_id"])
def test_parent_context_must_satisfy_filters_before_budget(
    budget: int | None,
    filter_kind: str,
) -> None:
    async def scenario() -> None:
        search, _, child = await search_fixture()
        filters: Filter = {
            "eq": Eq("visibility", "public"),
            "in": In("visibility", ("public",)),
            "and": And((Eq("source_id", "source"), Eq("visibility", "public"))),
            "unit_id": Eq("unit_id", child.unit_id),
        }[filter_kind]
        plain = await search.run("cat", mode="keyword", filters=filters)
        assert plain.contexts == (child,)
        with pytest.raises(ValueError, match="context.*filter"):
            await search.run(
                "cat",
                mode="keyword",
                filters=filters,
                expand_parents=True,
                context_budget=budget,
            )

    asyncio.run(scenario())


def test_allowed_parent_context_preserves_filtered_child_evidence() -> None:
    async def scenario() -> None:
        search, parent, child = await search_fixture(parent_visibility="public")
        found = await search.run(
            "cat",
            mode="keyword",
            filters=Eq("visibility", "public"),
            expand_parents=True,
        )
        assert found.contexts == (parent,)
        assert found.matched_units[0].unit == child
        assert found.matched_units[0].evidence[0].hit.unit_id == child.unit_id
        assert found.diagnostics == ()

    asyncio.run(scenario())


def test_stored_match_filters_checked_before_reranking() -> None:
    class UnexpectedReranker:
        async def rerank(self, query, candidates):
            pytest.fail("filtered-out content reached reranker")

    async def scenario() -> None:
        search, _, child = await search_fixture()
        original = search.docstore.get_units

        async def wrong_metadata(ids):
            units = await original(ids)
            return {
                key: replace(unit, metadata={"visibility": "restricted"})
                for key, unit in units.items()
            }

        search.docstore.get_units = wrong_metadata
        search.reranker = UnexpectedReranker()
        with pytest.raises(ValueError, match="content.*filter"):
            await search.run("cat", mode="keyword", filters=Eq("visibility", "public"))
        assert child.metadata["visibility"] == "public"

    asyncio.run(scenario())
