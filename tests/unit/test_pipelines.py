from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.content import ContentItem, ImagePart, MediaRef, text_view
from comet_rag.docstores.memory import AsyncMemoryDocStore
from comet_rag.documents.parsers.text import AsyncTextParser
from comet_rag.documents.schemas import Asset, ParsedDocument
from comet_rag.embedder.types import EmbeddingResult, EmbeddingSpace
from comet_rag.errors import IngestionIncompleteError
from comet_rag.indexing.builders.flat import FlatIndexBuilder
from comet_rag.indexing.types import BuildResult, IndexUnit, Representation
from comet_rag.pipelines import (
    AsyncIngestionPipeline,
    AsyncSearchPipeline,
    delete_document,
)
from comet_rag.sources.local import AsyncLocalSourceResolver
from comet_rag.sources.types import LocalSource
from comet_rag.vectorstores.filters import Eq
from comet_rag.vectorstores.memory import AsyncMemoryVectorStore
from comet_rag.vectorstores.types import VectorConfirmation


class ProbeEmbedder:
    def __init__(self) -> None:
        self.space = EmbeddingSpace("test-only", 2, "unit-test")
        self.calls: list[str] = []

    async def embed(
        self, items: Sequence[ContentItem], *, role: Literal["query", "document"]
    ) -> tuple[EmbeddingResult, ...]:
        self.calls.append(role)
        return tuple(
            EmbeddingResult(
                item.id,
                (1.0, 0.0) if "cat" in text_view(item.content) else (0.0, 1.0),
                self.space.space_id,
            )
            for item in items
        )


def components(
    tmp_path: Path, vectors_cls: type[AsyncMemoryVectorStore] = AsyncMemoryVectorStore
):
    model = ProbeEmbedder()
    builder = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(100)))
    docs = AsyncMemoryDocStore(index_scope="scope")
    vectors = vectors_cls(
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
    return pipeline, model, docs, vectors


def test_three_modes_keyword_without_model_and_explicit_delete(tmp_path: Path) -> None:
    async def scenario() -> None:
        pipeline, model, docs, vectors = components(tmp_path)
        path = tmp_path / "cat.txt"
        path.write_bytes(b"cat purring\r\n  ")
        result = await pipeline.run(LocalSource(path, source_id="cat"))
        assert result.database_committed and result.query_visible
        assert path.read_bytes() == b"cat purring\r\n  "
        assert (await docs.get_manifest(result.document_id)) is not None
        for mode in ("dense", "keyword", "hybrid"):
            search = AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=None if mode == "keyword" else model,
            )
            before = len(model.calls)
            found = await search.run("cat", mode=mode)
            assert len(found.matched_units) == 1
            assert text_view(found.matched_units[0].unit.content) == "cat purring\r\n  "
            if mode == "keyword":
                assert len(model.calls) == before
        retry = await pipeline.run(LocalSource(path, source_id="cat"))
        assert retry.manifest.manifest_id == result.manifest.manifest_id
        await delete_document(
            document_id=result.document_id, docstore=docs, vectorstore=vectors
        )
        assert await docs.get_manifest(result.document_id) is None
        assert (
            await vectors.verify_records(result.manifest.record_ids)
        ).missing == result.manifest.record_ids
        assert path.exists()

    asyncio.run(scenario())


def test_vector_response_loss_keeps_manifest_and_actual_inputs(tmp_path: Path) -> None:
    class LostResponse(AsyncMemoryVectorStore):
        async def upsert(self, records):
            await super().upsert(records)
            raise TimeoutError("accepted, response lost")

    async def scenario() -> None:
        pipeline, _, docs, vectors = components(tmp_path, LostResponse)
        path = tmp_path / "source.txt"
        path.write_text("cat")
        with pytest.raises(IngestionIncompleteError) as caught:
            await pipeline.run(LocalSource(path, source_id="cat"))
        error = caught.value
        assert isinstance(error, RuntimeError)
        assert isinstance(error.__cause__, TimeoutError)
        assert str(error.__cause__) == "accepted, response lost"
        result = error.result
        assert result.database_committed
        assert result.source_id == "cat"
        assert error.args == (
            f"vector state unknown or incomplete for {result.manifest.manifest_id}",
        )
        manifest = await docs.get_manifest(result.document_id)
        assert manifest is not None
        assert manifest.confirmations[-1].unknown_ids == manifest.record_ids
        assert set((await vectors.verify_records(manifest.record_ids)).present) == set(
            manifest.record_ids
        )
        assert set(await docs.get_representations(manifest.representation_ids)) == set(
            manifest.representation_ids
        )
        assert not result.query_visible

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_mode", ["before_write", "response_lost"])
def test_retry_replays_saved_vectors_without_calling_nondeterministic_model(
    tmp_path: Path, failure_mode: str
) -> None:
    class InterruptedWrite(AsyncMemoryVectorStore):
        attempts = 0

        async def upsert(self, records):
            self.attempts += 1
            if self.attempts == 1:
                if failure_mode == "response_lost":
                    await super().upsert(records)
                raise TimeoutError("write interrupted")
            return await super().upsert(records)

    async def scenario() -> None:
        pipeline, model, docs, vectors = components(tmp_path, InterruptedWrite)
        calls = 0

        async def drifting_embed(items, *, role):
            nonlocal calls
            calls += 1
            return tuple(
                EmbeddingResult(item.id, (1.0, calls * 1e-7), model.space.space_id)
                for item in items
            )

        model.embed = drifting_embed
        path = tmp_path / "source.txt"
        path.write_text("cat")
        source = LocalSource(path, source_id="cat")
        with pytest.raises(IngestionIncompleteError) as caught:
            await pipeline.run(source)
        first = caught.value.result.manifest
        assert tuple(first.record_vectors.values()) == ((1.0, 1e-7),)
        result = await pipeline.run(source)
        assert result.query_visible and calls == 1
        assert result.manifest.manifest_id == first.manifest_id
        assert result.manifest.record_vectors == first.record_vectors
        saved = await docs.get_manifest(result.document_id)
        assert saved is not None
        assert set(
            (
                await vectors.verify_records(
                    saved.record_ids, fingerprints=saved.record_fingerprints
                )
            ).present
        ) == set(saved.record_ids)
        again = await pipeline.run(source)
        assert again.query_visible and calls == 1

    asyncio.run(scenario())


def test_required_content_and_changed_snapshot_are_not_silently_replaced(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        pipeline, model, docs, vectors = components(tmp_path)
        path = tmp_path / "source.txt"
        path.write_text("cat")
        result = await pipeline.run(LocalSource(path, source_id="cat"))
        path.write_text("changed cat")
        with pytest.raises(ValueError):
            await pipeline.run(LocalSource(path, source_id="cat"))
        await docs.delete_units(result.manifest.unit_ids)
        with pytest.raises(ValueError, match="missing"):
            await AsyncSearchPipeline(
                docstore=docs, vectorstore=vectors, embedder=model
            ).run("cat", mode="dense")
        assert await docs.get_manifest(result.document_id) is not None

    asyncio.run(scenario())


def test_failed_vector_deletion_keeps_content_and_manifest(tmp_path: Path) -> None:
    class IncompleteDelete(AsyncMemoryVectorStore):
        async def delete(self, record_ids: Sequence[str]) -> VectorConfirmation:
            return VectorConfirmation(tuple(record_ids), unknown_ids=tuple(record_ids))

    async def scenario() -> None:
        pipeline, _, docs, vectors = components(tmp_path, IncompleteDelete)
        source = tmp_path / "source.txt"
        source.write_text("cat")
        result = await pipeline.run(LocalSource(source))
        with pytest.raises(RuntimeError, match="manifest retained"):
            await delete_document(
                document_id=result.document_id, docstore=docs, vectorstore=vectors
            )
        assert await docs.get_manifest(result.document_id) is not None
        assert set(await docs.get_units(result.manifest.unit_ids)) == set(
            result.manifest.unit_ids
        )
        assert set(
            await docs.get_representations(result.manifest.representation_ids)
        ) == set(result.manifest.representation_ids)
        assert set(
            (await vectors.verify_records(result.manifest.record_ids)).present
        ) == set(result.manifest.record_ids)

    asyncio.run(scenario())


def test_representation_assets_are_validated_before_embedding(tmp_path: Path) -> None:
    class UnretainedImageBuilder(FlatIndexBuilder):
        def build(
            self, document: ParsedDocument, *, index_scope: str, index_profile_id: str
        ) -> BuildResult:
            built = super().build(
                document, index_scope=index_scope, index_profile_id=index_profile_id
            )
            data = b"unretained media"
            path = tmp_path / "outside-scope.png"
            path.write_bytes(data)
            asset = Asset(
                "actual-image",
                sha256(data).hexdigest(),
                MediaRef(path=path, media_type="image/png"),
            )
            rep = replace(
                built.representations[0],
                content=(ImagePart(asset.media),),
                assets=(asset,),
            )
            return BuildResult(built.units, (rep,))

    async def scenario() -> None:
        pipeline, model, docs, _ = components(tmp_path)
        pipeline.builder = UnretainedImageBuilder(
            DocumentChunker(RecursiveTextChunker(100))
        )
        path = tmp_path / "source.txt"
        path.write_text("cat")
        with pytest.raises(ValueError, match="retained"):
            await pipeline.run(LocalSource(path))
        assert model.calls == []
        assert [manifest async for manifest in docs.iter_manifests()] == []

    asyncio.run(scenario())


def test_repeated_asset_ids_do_not_hide_a_changed_access_path(tmp_path: Path) -> None:
    from comet_rag.chunkers import SourceRef

    data = b"retained input"
    path = tmp_path / "retained.png"
    path.write_bytes(data)
    asset = Asset("same-asset", sha256(data).hexdigest(), MediaRef(path=path))
    unit = IndexUnit(
        "s",
        "p",
        "source",
        "document",
        "text",
        (SourceRef("document", "b"),),
        assets=(asset,),
    )
    changed = replace(asset, media=MediaRef(path=tmp_path / "missing.png"))
    rep = Representation(
        unit.unit_id, "s", "p", "source", "document", "text", assets=(changed,)
    )
    with pytest.raises(ValueError, match="retained"):
        AsyncIngestionPipeline._validate_assets((unit, rep), tmp_path)


def test_document_metadata_reaches_native_prefilter() -> None:
    from comet_rag.documents.schemas import (
        Block,
        ParsedDocument,
        ParsingInfo,
        RichText,
        SourceInfo,
        TextRun,
    )
    from comet_rag.pipelines.ingestion import _build_records

    async def scenario() -> None:
        model = ProbeEmbedder()
        builder = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(100)))
        doc = ParsedDocument(
            SourceInfo("source", "0" * 64),
            ParsingInfo("test"),
            (Block("b", "paragraph", rich_text=RichText((TextRun("cat"),))),),
            metadata={"category": "included"},
        )
        built = builder.build(
            doc, index_scope="scope", index_profile_id=builder.profile_id(model.space)
        )
        embeddings = await model.embed(
            tuple(
                ContentItem(rep.representation_id, rep.content)
                for rep in built.representations
            ),
            role="document",
        )
        records = _build_records(
            built.representations, embeddings, model.space, built.units
        )
        vectors = AsyncMemoryVectorStore(
            index_scope="scope",
            index_profile_id=builder.profile_id(model.space),
            space=model.space,
            field_types={"category": str},
        )
        await vectors.upsert(records)
        query = EmbeddingResult("query", (1.0, 0.0), model.space.space_id)
        assert (
            len(
                await vectors.search(query, filters=Eq("category", "included"), limit=1)
            )
            == 1
        )
        assert (
            await vectors.search(query, filters=Eq("category", "excluded"), limit=1)
            == ()
        )

    asyncio.run(scenario())
