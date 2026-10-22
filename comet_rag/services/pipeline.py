from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

from comet_rag.engines.concurrency import map_concurrently
from comet_rag.engines.documents.normalization import (
    DocumentNormalizationStrategy,
    MarkdownDocumentNormalizer,
)
from comet_rag.engines.embedding import async_batch
from comet_rag.engines.embedding.batch import embed_documents
from comet_rag.engines.offload import run_sync
from comet_rag.engines.pipelines.hooks import (
    AsyncHookProvider,
    AsyncPipelineHooks,
    HookProvider,
    PipelineHooks,
)
from comet_rag.engines.pipelines.types import Chunk, PipelineConfig, PipelineResult
from comet_rag.ports import (
    AsyncEmbeddingPort,
    AsyncSourceLoaderPort,
    EmbeddingPort,
    ExtractedDocument,
    NormalizedDocument,
    SourceLoaderPort,
)
from comet_rag.ports.source import LoadedResource, SourceContent
from comet_rag.services.chunking import ChunkingService, materialize_chunk_drafts

LoaderContent = LoadedResource


class _PipelineComputation:
    _config: PipelineConfig
    _chunking: ChunkingService

    def _chunk(
        self, lc: LoaderContent, file_type: str, document: NormalizedDocument
    ) -> list[Chunk]:
        drafts = self._chunking.split(file_type, document)
        source_id = lc.source.source_id
        materialized = materialize_chunk_drafts(
            drafts,
            source_id=source_id,
            source=lc.source.source,
            file_type=file_type,
            document_metadata=document.metadata,
        )
        return [
            Chunk(
                id=item.id,
                text=item.text,
                metadata=item.metadata,
            )
            for item in materialized
        ]

    def _build_result(
        self,
        lc: LoaderContent,
        chunks: list[Chunk],
        document: NormalizedDocument,
    ) -> PipelineResult:
        return PipelineResult(
            source_id=lc.source.source_id,
            file_type=lc.metadata.get("file_type", ""),
            chunks=chunks,
            metadata={
                **document.metadata,
                **{k: v for k, v in lc.metadata.items() if k != "parse_config"},
            },
        )

    def _windows(self, chunks: list[Chunk]) -> Iterator[list[Chunk]]:
        size = self._config.embed_batch_size
        for start in range(0, len(chunks), size):
            yield chunks[start : start + size]


class Pipeline(_PipelineComputation):
    def __init__(
        self,
        config: PipelineConfig | None = None,
        *,
        loader: SourceLoaderPort,
        embedding_model: EmbeddingPort | None = None,
        hooks: HookProvider | None = None,
        normalizer: DocumentNormalizationStrategy | None = None,
    ) -> None:
        self._config = config or PipelineConfig()
        self._loader = loader
        self._embedding_model = embedding_model
        self._hooks = hooks or PipelineHooks
        self._normalizer = normalizer or MarkdownDocumentNormalizer()
        self._chunking = ChunkingService(self._config, self._hooks)

    def run(self, source: str | Path | SourceContent) -> PipelineResult:
        lc = self._load(source)
        try:
            chunks, document = self._process(lc)
            if self._config.embed and self._embedding_model:
                self._embed_chunks(chunks)
            return self._build_result(lc, chunks, document)
        finally:
            lc.cleanup()

    def batch_run(
        self, sources: list[str | Path | SourceContent]
    ) -> list[PipelineResult]:
        from concurrent.futures import ThreadPoolExecutor

        if not sources:
            return []

        with ThreadPoolExecutor(
            max_workers=min(self._config.max_concurrency, len(sources))
        ) as ex:
            return [f.result() for f in [ex.submit(self.run, s) for s in sources]]

    def stream_run(self, source: str | Path | SourceContent) -> Iterator[Chunk]:
        lc = self._load(source)
        try:
            chunks, _ = self._process(lc)
            yield from self._iter_embedded(chunks)
        finally:
            lc.cleanup()

    def _process(self, lc: LoaderContent) -> tuple[list[Chunk], NormalizedDocument]:
        file_type, document = self._extract(lc)
        return self._chunk(lc, file_type, document), document

    def _extract(self, lc: LoaderContent) -> tuple[str, NormalizedDocument]:
        file_type = str(lc.metadata.get("file_type", "")).lower()
        extracted = self._hooks.extract(file_type, lc, self._config)
        return file_type, self._normalizer.normalize(extracted)

    def _load(self, source: str | Path | SourceContent) -> LoaderContent:
        if not isinstance(source, SourceContent):
            source = SourceContent(source)
        return self._loader.load(source)

    def _require_model(self) -> EmbeddingPort:
        if self._embedding_model is None:
            raise RuntimeError(
                "Embedding model is not initialized, cannot execute embedding."
            )
        return self._embedding_model

    def _iter_embedded(self, chunks: list[Chunk]) -> Iterator[Chunk]:
        """按窗口并发 embed，每窗完成即产出 —— 保住流式语义。"""
        if not (self._config.embed and self._embedding_model):
            yield from chunks
            return
        model = self._require_model()
        for window in self._windows(chunks):
            embeddings = embed_documents(
                model,
                [c.text for c in window],
                max_concurrency=self._config.max_concurrency,
            )
            for chunk, emb in zip(window, embeddings, strict=True):
                chunk.embedding = emb
                yield chunk

    def _embed_chunks(self, chunks: list[Chunk]) -> None:
        # 复用窗口化实现：一次性把所有 chunk 交给调度器会为超大文档
        # 同时创建上万个待处理项，窗口化把峰值占用限制在 embed_batch_size。
        for _ in self._iter_embedded(chunks):
            pass


class AsyncPipeline(_PipelineComputation):
    def __init__(
        self,
        config: PipelineConfig | None = None,
        *,
        loader: AsyncSourceLoaderPort,
        embedding_model: AsyncEmbeddingPort | None = None,
        hooks: AsyncHookProvider | None = None,
        normalizer: DocumentNormalizationStrategy | None = None,
    ) -> None:
        self._config = config or PipelineConfig()
        self._loader = loader
        self._embedding_model = embedding_model
        self._hooks = hooks or AsyncPipelineHooks
        self._normalizer = normalizer or MarkdownDocumentNormalizer()
        self._chunking = ChunkingService(self._config, self._hooks)

    async def run(self, source: str | Path | SourceContent) -> PipelineResult:
        lc = await self._load(source)
        try:
            chunks, document = await self._process(lc)
            if self._config.embed and self._embedding_model:
                await self._embed_chunks(chunks)
            return self._build_result(lc, chunks, document)
        finally:
            await run_sync(lc.cleanup)

    async def batch_run(
        self, sources: list[str | Path | SourceContent]
    ) -> list[PipelineResult]:
        return await map_concurrently(
            self.run, sources, limit=self._config.max_concurrency
        )

    async def stream_run(
        self, source: str | Path | SourceContent
    ) -> AsyncGenerator[Chunk, None]:
        lc = await self._load(source)
        try:
            chunks, _ = await self._process(lc)
            async for chunk in self._iter_embedded(chunks):
                yield chunk
        finally:
            await run_sync(lc.cleanup)

    async def _process(
        self, lc: LoaderContent
    ) -> tuple[list[Chunk], NormalizedDocument]:
        file_type, extracted = await self._extract(lc)
        document = await run_sync(self._normalizer.normalize, extracted)
        chunks = await run_sync(self._chunk, lc, file_type, document)
        return chunks, document

    async def _extract(self, lc: LoaderContent) -> tuple[str, ExtractedDocument]:
        file_type = str(lc.metadata.get("file_type", "")).lower()
        document = await self._hooks.extract(file_type, lc, self._config)
        return file_type, document

    async def _load(self, source: str | Path | SourceContent) -> LoaderContent:
        if not isinstance(source, SourceContent):
            source = SourceContent(source)
        return await self._loader.load(source)

    def _require_model(self) -> AsyncEmbeddingPort:
        if self._embedding_model is None:
            raise RuntimeError(
                "Embedding model is not initialized, cannot execute embedding."
            )
        return self._embedding_model

    async def _iter_embedded(self, chunks: list[Chunk]) -> AsyncGenerator[Chunk, None]:
        if not (self._config.embed and self._embedding_model):
            for chunk in chunks:
                yield chunk
            return
        model = self._require_model()
        for window in self._windows(chunks):
            embeddings = await async_batch.embed_documents(
                model,
                [c.text for c in window],
                max_concurrency=self._config.max_concurrency,
            )
            for chunk, emb in zip(window, embeddings, strict=True):
                chunk.embedding = emb
                yield chunk

    async def _embed_chunks(self, chunks: list[Chunk]) -> None:
        async for _ in self._iter_embedded(chunks):
            pass
