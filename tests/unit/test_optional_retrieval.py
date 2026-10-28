from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker, SourceRef
from comet_rag.content import ContentInput, ContentItem, text_view
from comet_rag.docstores.memory import AsyncMemoryDocStore
from comet_rag.documents.parsers import AsyncTextParser
from comet_rag.documents.schemas import (
    Block,
    ParsedDocument,
    ParsingInfo,
    RichText,
    SourceInfo,
    TextRun,
)
from comet_rag.indexing.builders.flat import FlatIndexBuilder
from comet_rag.indexing.builders.parent_child import ParentChildIndexBuilder
from comet_rag.indexing.types import IndexUnit, Representation
from comet_rag.pipelines import AsyncIngestionPipeline, AsyncSearchPipeline
from comet_rag.rerankers import RankedItem, RerankResult
from comet_rag.retrieval.context import select_contexts
from comet_rag.sources import AsyncLocalSourceResolver, LocalSource
from comet_rag.vectorstores.memory import AsyncMemoryVectorStore
from tests.unit.test_pipelines import ProbeEmbedder, components


class ReverseReranker:
    async def rerank(
        self, query: ContentInput, candidates: Sequence[ContentItem]
    ) -> RerankResult:
        return RerankResult(
            tuple(
                RankedItem(item.id, i, float(len(candidates) - i))
                for i, item in enumerate(reversed(candidates), 1)
            ),
            "test-only",
            "test-score",
        )


class BrokenReranker:
    async def rerank(
        self, query: ContentInput, candidates: Sequence[ContentItem]
    ) -> RerankResult:
        raise TimeoutError("test failure")


class CancelledReranker:
    async def rerank(
        self, query: ContentInput, candidates: Sequence[ContentItem]
    ) -> RerankResult:
        raise asyncio.CancelledError()


def test_rerank_success_never_pads_unscored_tail_and_failure_returns_original(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = ProbeEmbedder()
        builder = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(100)))
        docs = AsyncMemoryDocStore(index_scope="scope")
        vectors = AsyncMemoryVectorStore(
            index_scope="scope",
            index_profile_id=builder.profile_id(model.space),
            space=model.space,
        )
        pipeline = AsyncIngestionPipeline(
            resolver=AsyncLocalSourceResolver(),
            parser=AsyncTextParser(),
            builder=builder,
            embedder=model,
            docstore=docs,
            vectorstore=vectors,
            output_dir=tmp_path / "assets",
        )
        for name in ("cat first", "cat second", "cat third"):
            path = tmp_path / (name + ".txt")
            path.write_text(name)
            await pipeline.run(LocalSource(path, source_id=name))
        plain = await AsyncSearchPipeline(
            docstore=docs, vectorstore=vectors, embedder=model
        ).run("cat", mode="dense", top_k=3)
        successful = await AsyncSearchPipeline(
            docstore=docs,
            vectorstore=vectors,
            embedder=model,
            reranker=ReverseReranker(),
        ).run("cat", mode="dense", rerank_limit=2, top_k=3)
        assert len(successful.matched_units) == 2 and successful.reranked
        assert [m.unit.unit_id for m in successful.matched_units] == [
            m.unit.unit_id for m in reversed(plain.matched_units[:2])
        ]
        assert "rerank_limit_restricts_matches" in successful.diagnostics
        assert successful.rerank_result is not None
        assert all(
            m.rerank_score is not None and m.evidence for m in successful.matched_units
        )
        fallback = await AsyncSearchPipeline(
            docstore=docs,
            vectorstore=vectors,
            embedder=model,
            reranker=BrokenReranker(),
        ).run("cat", mode="dense", rerank_limit=1, top_k=3)
        assert fallback.matched_units == plain.matched_units and not fallback.reranked
        assert fallback.diagnostics == ("rerank_failed:TimeoutError",)
        with pytest.raises(TimeoutError):
            await AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=model,
                reranker=BrokenReranker(),
                strict_rerank=True,
            ).run("cat", mode="dense")
        with pytest.raises(asyncio.CancelledError):
            await AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=model,
                reranker=CancelledReranker(),
            ).run("cat", mode="dense")
        await docs.delete_units([plain.matched_units[0].unit.unit_id])
        with pytest.raises(ValueError, match="missing"):
            await AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=model,
                reranker=BrokenReranker(),
            ).run("cat", mode="dense")

    asyncio.run(scenario())


def test_parent_content_persists_without_vectors_and_shared_context_is_deduplicated(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = ProbeEmbedder()
        builder = ParentChildIndexBuilder(
            parent=FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(100))),
            child=FlatIndexBuilder(
                DocumentChunker(RecursiveTextChunker(4, separators=("",)))
            ),
        )
        docs = AsyncMemoryDocStore(index_scope="scope")
        vectors = AsyncMemoryVectorStore(
            index_scope="scope",
            index_profile_id=builder.profile_id(model.space),
            space=model.space,
        )
        pipeline = AsyncIngestionPipeline(
            resolver=AsyncLocalSourceResolver(),
            parser=AsyncTextParser(),
            builder=builder,
            embedder=model,
            docstore=docs,
            vectorstore=vectors,
            output_dir=tmp_path / "assets",
        )
        path = tmp_path / "sample.txt"
        path.write_text("cat cat cat cat")
        result = await pipeline.run(LocalSource(path))
        assert (
            len(result.manifest.unit_ids) == len(result.manifest.representation_ids) + 1
        )
        search = AsyncSearchPipeline(docstore=docs, vectorstore=vectors, embedder=model)
        found = await search.run("cat", mode="dense", top_k=2, expand_parents=True)
        assert len(found.matched_units) == 2 and len(found.contexts) == 1
        assert all(
            match.unit.parent_unit_id == found.contexts[0].unit_id
            for match in found.matched_units
        )
        assert text_view(found.contexts[0].content) == "cat cat cat cat"
        limited = await search.run(
            "cat", mode="dense", top_k=2, expand_parents=True, context_budget=4
        )
        assert limited.contexts == () and len(limited.matched_units) == 2
        assert limited.diagnostics == ("context_budget_omitted:1",)
        invalid = await search.run("cat", mode="dense", context_budget=0)
        assert invalid.contexts == ()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("parent_size", "parent_overlap", "child_size"),
    [(5, 0, 4), (6, 2, 2)],
)
def test_ambiguous_or_crossing_child_ranges_are_rejected(
    parent_size: int,
    parent_overlap: int,
    child_size: int,
) -> None:
    document = ParsedDocument(
        SourceInfo("s", "0" * 64),
        ParsingInfo("test"),
        (Block("b", "paragraph", rich_text=RichText((TextRun("0123456789"),))),),
    )
    builder = ParentChildIndexBuilder(
        parent=FlatIndexBuilder(
            DocumentChunker(
                RecursiveTextChunker(parent_size, parent_overlap, separators=("",))
            )
        ),
        child=FlatIndexBuilder(
            DocumentChunker(RecursiveTextChunker(child_size, separators=("",)))
        ),
    )
    parents = builder.parent.build(document, index_scope="s", index_profile_id="p")
    children = builder.child.build(document, index_scope="s", index_profile_id="p")
    if parent_overlap:
        assert [
            (unit.source_refs[0].start, unit.source_refs[0].end)
            for unit in parents.units
        ] == [(0, 6), (4, 10)]
        assert [(ref.start, ref.end) for ref in children.units[2].source_refs] == [
            (4, 6)
        ]
    else:
        assert [
            (unit.source_refs[0].start, unit.source_refs[0].end)
            for unit in parents.units
        ] == [(0, 5), (5, 10)]
        assert [(ref.start, ref.end) for ref in children.units[1].source_refs] == [
            (4, 8)
        ]
    with pytest.raises(ValueError, match="containing parent"):
        builder.build(document, index_scope="s", index_profile_id="p")


def test_parent_child_builder_preserves_caller_metadata() -> None:
    document = ParsedDocument(
        SourceInfo("s", "0" * 64),
        ParsingInfo("test"),
        (Block("b", "paragraph", rich_text=RichText((TextRun("0123456789"),))),),
        metadata={"index_role": "customer-value", "nested": {"kind": "original"}},
    )
    builder = ParentChildIndexBuilder(
        parent=FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(20))),
        child=FlatIndexBuilder(
            DocumentChunker(RecursiveTextChunker(5, separators=("",)))
        ),
    )
    built = builder.build(document, index_scope="s", index_profile_id="p")
    assert len(built.units) == 3
    assert all(unit.metadata == document.metadata for unit in built.units)
    assert built.units[1].parent_unit_id == built.units[0].unit_id
    assert built.units[2].parent_unit_id == built.units[0].unit_id


def context_units() -> tuple[IndexUnit, IndexUnit, IndexUnit, IndexUnit]:
    parent = IndexUnit(
        "scope",
        "profile",
        "source",
        "document",
        "0123456789",
        (SourceRef("document", "b"),),
    )
    first = IndexUnit(
        "scope",
        "profile",
        "source",
        "document",
        "012",
        (SourceRef("document", "b", start=0, end=3),),
        parent_unit_id=parent.unit_id,
    )
    second = IndexUnit(
        "scope",
        "profile",
        "source",
        "document",
        "345",
        (SourceRef("document", "b", start=3, end=6),),
        parent_unit_id=parent.unit_id,
        ordinal=1,
    )
    standalone = IndexUnit(
        "scope",
        "profile",
        "source",
        "document",
        "xy",
        (SourceRef("document", "other"),),
        ordinal=2,
    )
    return parent, first, second, standalone


class MappedDocStore(AsyncMemoryDocStore):
    def __init__(self, mapping: dict[str, IndexUnit]) -> None:
        super().__init__(index_scope="scope")
        self.mapping = mapping
        self.requests: list[tuple[str, ...]] = []

    async def get_units(self, ids: Sequence[str]) -> dict[str, IndexUnit]:
        self.requests.append(tuple(ids))
        return self.mapping


@pytest.mark.parametrize(
    "fault",
    ["missing", "wrong_key", "wrong_unit_id", "snapshot", "scope", "profile", "source"],
)
def test_context_rejects_missing_miskeyed_and_foreign_parent_before_budget(
    fault: str,
) -> None:
    parent, first, _, standalone = context_units()
    mapping = {parent.unit_id: parent}
    if fault == "missing":
        mapping = {}
    elif fault == "wrong_key":
        mapping = {standalone.unit_id: parent}
    elif fault == "wrong_unit_id":
        mapping = {parent.unit_id: replace(parent, ordinal=20)}
    else:
        field = {
            "snapshot": "document_id",
            "scope": "index_scope",
            "profile": "index_profile_id",
            "source": "source_id",
        }[fault]
        updates: dict[str, Any] = {field: "foreign"}
        if fault == "snapshot":
            updates["source_refs"] = (SourceRef("foreign", "b"),)
        foreign = replace(parent, **updates)
        first = replace(first, parent_unit_id=foreign.unit_id)
        mapping = {foreign.unit_id: foreign}

    async def scenario() -> None:
        with pytest.raises(ValueError, match="parent context"):
            await select_contexts(
                (first,),
                docstore=MappedDocStore(mapping),
                expand_parents=True,
                budget=0,
            )

    asyncio.run(scenario())


def test_context_budget_counts_complete_unique_units_and_keeps_selection_order() -> (
    None
):
    parent, first, second, standalone = context_units()

    async def scenario() -> None:
        docs = MappedDocStore({parent.unit_id: parent})
        selected, diagnostics = await select_contexts(
            (first, second, standalone), docstore=docs, expand_parents=True, budget=12
        )
        assert selected == (parent, standalone) and diagnostics == ()
        assert docs.requests == [(parent.unit_id,)]
        limited, diagnostics = await select_contexts(
            (first, second, standalone), docstore=docs, expand_parents=True, budget=2
        )
        assert limited == (standalone,) and diagnostics == ("context_budget_omitted:1",)
        assert (
            text_view(parent.content) == "0123456789"
            and text_view(standalone.content) == "xy"
        )
        selected, diagnostics = await select_contexts(
            (first, first, second, standalone),
            docstore=docs,
            expand_parents=False,
            budget=5,
        )
        assert selected == (first, standalone) and diagnostics == (
            "context_budget_omitted:1",
        )
        selected, diagnostics = await select_contexts(
            (first, second), docstore=docs, expand_parents=True, budget=0
        )
        assert selected == () and diagnostics == ("context_budget_omitted:1",)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", ["missing", "foreign", "duplicate", "rank", "nonfinite"]
)
def test_malformed_rerank_full_coverage_defaults_to_fallback_and_strict_fails(
    tmp_path: Path, fault: str
) -> None:
    class MalformedReranker:
        async def rerank(
            self, query: ContentInput, candidates: Sequence[ContentItem]
        ) -> RerankResult:
            first, second = candidates
            if fault == "missing":
                items = (RankedItem(first.id, 1, 1.0),)
            elif fault == "foreign":
                items = (RankedItem(first.id, 1, 1.0), RankedItem("foreign", 2, 0.0))
            elif fault == "duplicate":
                items = (RankedItem(first.id, 1, 1.0), RankedItem(first.id, 2, 0.0))
            elif fault == "rank":
                items = (RankedItem(first.id, 2, 1.0), RankedItem(second.id, 1, 0.0))
            else:
                items = (
                    RankedItem(first.id, 1, float("nan")),
                    RankedItem(second.id, 2, 0.0),
                )
            return RerankResult(items, "test-only", "logit")

    async def scenario() -> None:
        ingestion, model, docs, vectors = components(tmp_path)
        for index in range(3):
            path = tmp_path / f"source-{index}.txt"
            path.write_text(f"cat {index}")
            await ingestion.run(LocalSource(path))
        plain = await AsyncSearchPipeline(
            docstore=docs, vectorstore=vectors, embedder=model
        ).run("cat", mode="dense", top_k=3)
        fallback = await AsyncSearchPipeline(
            docstore=docs,
            vectorstore=vectors,
            embedder=model,
            reranker=MalformedReranker(),
        ).run("cat", mode="dense", top_k=3, rerank_limit=2)
        assert (
            fallback.matched_units == plain.matched_units
            and fallback.contexts == plain.contexts
        )
        assert fallback.rerank_result is None and not fallback.reranked
        assert fallback.diagnostics == ("rerank_failed:ValueError",)
        with pytest.raises(ValueError):
            await AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=model,
                reranker=MalformedReranker(),
                strict_rerank=True,
            ).run("cat", mode="dense", top_k=3, rerank_limit=2)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "representation_missing",
        "representation_identity",
        "unit_missing",
        "unit_identity",
        "snapshot",
    ],
)
def test_content_consistency_is_checked_before_rerank_fallback(
    tmp_path: Path, fault: str
) -> None:
    class CorruptDocStore(AsyncMemoryDocStore):
        async def get_units(self, ids: Sequence[str]) -> dict[str, IndexUnit]:
            result = await super().get_units(ids)
            if fault == "unit_missing":
                return {}
            if fault == "unit_identity":
                return {
                    key: replace(unit, ordinal=unit.ordinal + 10)
                    for key, unit in result.items()
                }
            if fault == "snapshot":
                return {
                    key: replace(
                        unit,
                        document_id="foreign",
                        source_refs=(SourceRef("foreign", "b"),),
                    )
                    for key, unit in result.items()
                }
            return result

        async def get_representations(
            self, ids: Sequence[str]
        ) -> dict[str, Representation]:
            result = await super().get_representations(ids)
            if fault == "representation_missing":
                return {}
            if fault == "representation_identity":
                return {
                    key: replace(rep, purpose="other-actual-representation")
                    for key, rep in result.items()
                }
            return result

    class ProbeReranker:
        calls = 0

        async def rerank(
            self, query: ContentInput, candidates: Sequence[ContentItem]
        ) -> RerankResult:
            self.calls += 1
            raise TimeoutError("must not reach rerank")

    async def scenario() -> None:
        ingestion, model, _, vectors = components(tmp_path)
        docs = CorruptDocStore(index_scope="scope")
        ingestion.docstore = docs
        path = tmp_path / "source.txt"
        path.write_text("cat")
        await ingestion.run(LocalSource(path))
        reranker = ProbeReranker()
        with pytest.raises(ValueError, match="identity|identities|missing"):
            await AsyncSearchPipeline(
                docstore=docs, vectorstore=vectors, embedder=model, reranker=reranker
            ).run("cat", mode="dense")
        assert reranker.calls == 0

    asyncio.run(scenario())


def test_active_rerank_cancellation_propagates_in_default_and_strict_modes(
    tmp_path: Path,
) -> None:
    class BlockingReranker:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.cancelled = False

        async def rerank(
            self, query: ContentInput, candidates: Sequence[ContentItem]
        ) -> RerankResult:
            self.entered.set()
            try:
                await asyncio.Future()
            finally:
                self.cancelled = True
            raise AssertionError("unreachable rerank result")

    async def scenario() -> None:
        ingestion, model, docs, vectors = components(tmp_path)
        path = tmp_path / "source.txt"
        path.write_text("cat")
        await ingestion.run(LocalSource(path))
        for strict in (False, True):
            reranker = BlockingReranker()
            search = AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=model,
                reranker=reranker,
                strict_rerank=strict,
            )
            task = asyncio.create_task(search.run("cat", mode="dense"))
            await asyncio.wait_for(reranker.entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert reranker.cancelled

    asyncio.run(scenario())


def test_parent_context_consistency_failure_is_not_hidden_after_rerank_fallback(
    tmp_path: Path,
) -> None:
    class CorruptParents(AsyncMemoryDocStore):
        async def get_units(self, ids: Sequence[str]) -> dict[str, IndexUnit]:
            result = await super().get_units(ids)
            return {
                key: replace(unit, ordinal=unit.ordinal + 100)
                if unit.parent_unit_id is None
                else unit
                for key, unit in result.items()
            }

    async def scenario() -> None:
        model = ProbeEmbedder()
        builder = ParentChildIndexBuilder(
            parent=FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(100))),
            child=FlatIndexBuilder(
                DocumentChunker(RecursiveTextChunker(4, separators=("",)))
            ),
        )
        docs = CorruptParents(index_scope="scope")
        vectors = AsyncMemoryVectorStore(
            index_scope="scope",
            index_profile_id=builder.profile_id(model.space),
            space=model.space,
        )
        ingestion = AsyncIngestionPipeline(
            resolver=AsyncLocalSourceResolver(),
            parser=AsyncTextParser(),
            builder=builder,
            embedder=model,
            docstore=docs,
            vectorstore=vectors,
            output_dir=tmp_path / "assets",
        )
        path = tmp_path / "source.txt"
        path.write_text("cat cat cat")
        await ingestion.run(LocalSource(path))
        with pytest.raises(ValueError, match="parent context identity"):
            await AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=model,
                reranker=BrokenReranker(),
            ).run("cat", mode="dense", expand_parents=True, context_budget=0)

    asyncio.run(scenario())
