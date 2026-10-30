from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from comet_rag._async import run_sync
from comet_rag.content import ContentItem, text_view
from comet_rag.docstores.base import AsyncDocStore
from comet_rag.docstores.types import WriteManifest, content_digest
from comet_rag.documents.parsers.base import AsyncDocumentParser
from comet_rag.embedder.types import (
    AsyncContentEmbedder,
    EmbeddingResult,
    EmbeddingSpace,
)
from comet_rag.identity import file_hash, stable_id
from comet_rag.indexing.base import IndexBuilder
from comet_rag.indexing.types import IndexUnit, Representation
from comet_rag.sources.base import AsyncSourceResolver
from comet_rag.sources.types import Source
from comet_rag.vectorstores.base import AsyncVectorStore
from comet_rag.vectorstores.types import VectorConfirmation, VectorRecord

from .types import IngestionIncomplete, IngestionResult


def _build_records(
    representations: Sequence[Representation],
    embeddings: Sequence[EmbeddingResult],
    space: EmbeddingSpace,
    units: Sequence[IndexUnit],
) -> tuple[VectorRecord, ...]:
    if tuple(item.item_id for item in embeddings) != tuple(
        rep.representation_id for rep in representations
    ):
        raise ValueError("embedding batch IDs/count/order do not match representations")
    if any(item.space_id != space.space_id for item in embeddings):
        raise ValueError("embedding space mismatch")
    unit_by_id = {unit.unit_id: unit for unit in units}
    if any(rep.unit_id not in unit_by_id for rep in representations):
        raise ValueError("representation refers to missing unit")
    records = []
    for rep, embedding in zip(representations, embeddings, strict=True):
        record = VectorRecord(
            rep.representation_id,
            rep.unit_id,
            rep.source_id,
            rep.document_id,
            rep.index_scope,
            rep.index_profile_id,
            space.space_id,
            embedding.vector,
            text_view(rep.content),
            metadata=unit_by_id[rep.unit_id].metadata,
        )
        record.validate(
            index_scope=rep.index_scope,
            index_profile_id=rep.index_profile_id,
            space=space,
        )
        records.append(record)
    return tuple(records)


class AsyncIngestionPipeline:
    def __init__(
        self,
        *,
        resolver: AsyncSourceResolver,
        parser: AsyncDocumentParser,
        builder: IndexBuilder,
        embedder: AsyncContentEmbedder,
        docstore: AsyncDocStore,
        vectorstore: AsyncVectorStore,
        output_dir: Path,
        accept_partial: bool = False,
    ) -> None:
        self.resolver = resolver
        self.parser = parser
        self.builder = builder
        self.embedder = embedder
        self.docstore = docstore
        self.vectorstore = vectorstore
        self.output_dir = Path(output_dir)
        if type(accept_partial) is not bool:
            raise ValueError("accept_partial must be boolean")
        self.accept_partial = accept_partial
        if docstore.index_scope != vectorstore.index_scope:
            raise ValueError("pipeline scope/profile/space mismatch")
        if builder.profile_id(embedder.space) != vectorstore.index_profile_id:
            raise ValueError("pipeline scope/profile/space mismatch")
        if embedder.space.space_id != vectorstore.space.space_id:
            raise ValueError("pipeline scope/profile/space mismatch")

    async def run(self, source: Source) -> IngestionResult:
        scope = self.vectorstore.index_scope
        profile = self.vectorstore.index_profile_id
        # scope 仅作身份，目录名使用固定摘要，避免调用方标识改变输出根位置。
        output_dir = self.output_dir / stable_id("scope", scope)
        async with self.resolver.resolve(source) as resource:
            parsed = await self.parser.parse(resource, output_dir=output_dir)
            if parsed.report.status == "partial":
                if not self.accept_partial:
                    raise ValueError("partial parsing requires explicit acceptance")
            built = self.builder.build(
                parsed.document, index_scope=scope, index_profile_id=profile
            )
            await run_sync(
                self._validate_assets,
                (*built.units, *built.representations),
                output_dir,
            )
            existing = await self.docstore.get_manifest(parsed.document.document_id)
            if existing is not None:
                if existing.content_fingerprint != content_digest(
                    built.units, built.representations
                ):
                    raise ValueError("retry content conflicts with stored manifest")
                stored = await self.docstore.get_representations(
                    existing.representation_ids
                )
                if set(stored) != set(existing.representation_ids):
                    raise ValueError("stored retry representations are incomplete")
                representations = tuple(
                    stored[item_id] for item_id in existing.representation_ids
                )
                await run_sync(self._validate_assets, representations, output_dir)
                if (existing.index_profile_id, existing.space_id) != (
                    profile,
                    self.embedder.space.space_id,
                ):
                    raise ValueError(
                        "retry profile/space conflicts with stored manifest"
                    )
                records = existing.replay_records(built.units, representations)
                for record in records:
                    record.validate(
                        index_scope=scope,
                        index_profile_id=profile,
                        space=self.embedder.space,
                    )
            else:
                representations = built.representations
                embeddings = await self.embedder.embed(
                    tuple(
                        ContentItem(rep.representation_id, rep.content)
                        for rep in representations
                    ),
                    role="document",
                )
                records = _build_records(
                    representations, embeddings, self.embedder.space, built.units
                )
            manifest = existing or WriteManifest(
                scope,
                profile,
                resource.source.source_id,
                parsed.document.document_id,
                self.embedder.space.space_id,
                tuple(unit.unit_id for unit in built.units),
                tuple(rep.representation_id for rep in representations),
                {record.record_id: record.fingerprint for record in records},
                content_digest(built.units, representations),
                {record.record_id: record.vector for record in records},
            )
            await self.docstore.upsert_units(
                built.units, representations=representations, manifest=manifest
            )
            result = IngestionResult(
                resource.source.source_id,
                parsed.document.document_id,
                scope,
                profile,
                manifest,
                parsed.report,
                True,
                False,
            )
            try:
                await self.vectorstore.prepare()
                confirmation = await self.vectorstore.upsert(records)
                await self.docstore.record_vector_confirmation(
                    manifest.document_id, confirmation
                )
                verification = await self.vectorstore.verify_records(
                    manifest.record_ids, fingerprints=manifest.record_fingerprints
                )
                if set(verification.present) != set(manifest.record_ids):
                    raise RuntimeError("expected records are not query-visible")
                if verification.missing:
                    raise RuntimeError("expected records are not query-visible")
                if verification.unknown:
                    raise RuntimeError("expected records are not query-visible")
                visible = VectorConfirmation(
                    manifest.record_ids,
                    accepted=True,
                    durable=confirmation.durable,
                    query_visible=True,
                    guarantee=verification.guarantee,
                )
                await self.docstore.record_vector_confirmation(
                    manifest.document_id, visible
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                unknown = VectorConfirmation(
                    manifest.record_ids, unknown_ids=manifest.record_ids
                )
                await self.docstore.record_vector_confirmation(
                    manifest.document_id, unknown
                )
                current = await self.docstore.get_manifest(manifest.document_id)
                raise IngestionIncomplete(
                    replace(result, manifest=current or manifest)
                ) from exc
            current = await self.docstore.get_manifest(manifest.document_id)
            return replace(result, manifest=current or manifest, query_visible=True)

    @staticmethod
    def _validate_assets(
        values: Sequence[IndexUnit | Representation], output_dir: Path
    ) -> None:
        for value in values:
            if not isinstance(value, (IndexUnit, Representation)):
                raise ValueError("builder did not return materialized content")
            for asset in value.assets:
                if asset.media.path is not None:
                    path = Path(asset.media.path).resolve()
                    if not path.is_relative_to(output_dir.resolve()):
                        raise ValueError(
                            "asset must be retained with matching bytes under scope output directory"
                        )
                    if not path.is_file():
                        raise ValueError(
                            "asset must be retained with matching bytes under scope output directory"
                        )
                    if file_hash(path) != asset.content_hash:
                        raise ValueError(
                            "asset must be retained with matching bytes under scope output directory"
                        )
                elif asset.media.data is None:
                    raise ValueError(
                        "persistent assets require retained files or inline bytes"
                    )


async def delete_document(
    *, document_id: str, docstore: AsyncDocStore, vectorstore: AsyncVectorStore
) -> None:
    if docstore.index_scope != vectorstore.index_scope:
        raise ValueError("deletion scope mismatch")
    manifest = await docstore.get_manifest(document_id)
    if manifest is None:
        return
    if (manifest.space_id, manifest.index_profile_id) != (
        vectorstore.space.space_id,
        vectorstore.index_profile_id,
    ):
        raise ValueError("deletion profile/space mismatch")
    await vectorstore.delete(manifest.record_ids)
    verification = await vectorstore.verify_records(manifest.record_ids)
    if set(verification.missing) != set(manifest.record_ids):
        raise RuntimeError("vector deletion is not confirmed; manifest retained")
    if verification.unknown:
        raise RuntimeError("vector deletion is not confirmed; manifest retained")
    if verification.present:
        raise RuntimeError("vector deletion is not confirmed; manifest retained")
    await docstore.delete_units(manifest.unit_ids)
    await docstore.delete_manifest(document_id)
