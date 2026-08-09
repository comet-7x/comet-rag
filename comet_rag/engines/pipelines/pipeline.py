from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from comet_rag.engines.loaders.auto_loader import AutoLoader
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.pipelines.hooks import PipelineHooks
from comet_rag.engines.pipelines.types import Chunk, PipelineConfig, PipelineResult
from comet_rag.engines.utils import compute_sha256

if TYPE_CHECKING:
    from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel


class Pipeline:
    def __init__(
        self,
        config: PipelineConfig | None = None,
        loader: BaseLoader | None = None,
        embedding_model: BaseEmbeddingModel | None = None,
    ):
        self._config = config or PipelineConfig()
        self._loader = loader or AutoLoader()
        self._embedding_model = embedding_model

    def run(self, source: str | Path | SourceContent) -> PipelineResult:
        lc = self._load(source)
        try:
            chunks = self._process(lc)
            if self._config.embed and self._embedding_model:
                self._embed_chunks(chunks)
            return self._build_result(lc, chunks)
        finally:
            lc.cleanup()

    async def arun(self, source: str | Path | SourceContent) -> PipelineResult:
        lc = await self._aload(source)
        try:
            chunks = await asyncio.to_thread(self._process, lc)
            if self._config.embed and self._embedding_model:
                await self._aembed_chunks(chunks)
            return self._build_result(lc, chunks)
        finally:
            lc.cleanup()

    def batch_run(
        self, sources: list[str | Path | SourceContent]
    ) -> list[PipelineResult]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=min(self._config.max_concurrency, len(sources))
        ) as ex:
            return [f.result() for f in [ex.submit(self.run, s) for s in sources]]

    async def abatch_run(
        self, sources: list[str | Path | SourceContent]
    ) -> list[PipelineResult]:
        sem = asyncio.Semaphore(self._config.max_concurrency)

        async def _guarded(s: str | Path | SourceContent) -> PipelineResult:
            async with sem:
                return await self.arun(s)

        return list(await asyncio.gather(*[_guarded(s) for s in sources]))

    def stream_run(self, source: str | Path | SourceContent) -> Iterator[Chunk]:
        lc = self._load(source)
        try:
            for chunk in self._process(lc):
                if self._config.embed and self._embedding_model:
                    chunk.embedding = self._embedding_model.embed(chunk.text)
                yield chunk
        finally:
            lc.cleanup()

    async def astream_run(
        self, source: str | Path | SourceContent
    ) -> AsyncGenerator[Chunk, None]:
        lc = await self._aload(source)
        try:
            chunks = await asyncio.to_thread(self._process, lc)
            for chunk in chunks:
                if self._config.embed and self._embedding_model:
                    chunk.embedding = await self._embedding_model.aembed(chunk.text)
                yield chunk
        finally:
            lc.cleanup()

    def _process(self, lc: LoaderContent) -> list[Chunk]:
        file_type = lc.metadata.get("file_type", "").lower()
        text = PipelineHooks.get_extractor(file_type)(lc, self._config)
        texts = PipelineHooks.get_chunker(file_type)(text, self._config)
        source_id = lc.source.source_id
        base_meta = {
            "source": lc.source.source,
            "source_id": source_id,
            "file_type": file_type,
            "total_chunks": len(texts),
        }
        return [
            Chunk(
                id=compute_sha256(f"{source_id}:{i}"),
                text=t,
                metadata={**base_meta, "chunk_index": i},
            )
            for i, t in enumerate(texts)
        ]

    def _load(self, source: str | Path | SourceContent) -> LoaderContent:
        if not isinstance(source, SourceContent):
            source = SourceContent(source)
        return self._loader.load(source)

    async def _aload(self, source: str | Path | SourceContent) -> LoaderContent:
        if not isinstance(source, SourceContent):
            source = SourceContent(source)
        return await self._loader.aload(source)

    def _build_result(self, lc: LoaderContent, chunks: list[Chunk]) -> PipelineResult:
        return PipelineResult(
            source_id=lc.source.source_id,
            file_type=lc.metadata.get("file_type", ""),
            chunks=chunks,
            metadata={k: v for k, v in lc.metadata.items() if k != "parse_config"},
        )

    def _embed_chunks(self, chunks: list[Chunk]) -> None:
        if self._embedding_model is None:
            raise RuntimeError(
                "Embedding model is not initialized, cannot execute embedding."
            )
        embeddings = self._embedding_model.batch_embed(
            [c.text for c in chunks], max_concurrency=self._config.max_concurrency
        )
        for chunk, emb in zip(chunks, embeddings, strict=True):
            chunk.embedding = emb

    async def _aembed_chunks(self, chunks: list[Chunk]) -> None:
        if self._embedding_model is None:
            raise RuntimeError(
                "Embedding model is not initialized, cannot execute embedding."
            )
        embeddings = await self._embedding_model.abatch_embed(
            [c.text for c in chunks], max_concurrency=self._config.max_concurrency
        )
        for chunk, emb in zip(chunks, embeddings, strict=True):
            chunk.embedding = emb
