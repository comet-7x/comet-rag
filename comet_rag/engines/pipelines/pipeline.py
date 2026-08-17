from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from comet_rag.application.embedding_batch import aembed_documents, embed_documents
from comet_rag.engines.loaders.auto_loader import AutoLoader
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.pipelines.hooks import PipelineHooks
from comet_rag.engines.pipelines.types import Chunk, PipelineConfig, PipelineResult
from comet_rag.engines.utils import compute_sha256

if TYPE_CHECKING:
    from comet_rag.application.ports import EmbeddingPort


class Pipeline:
    def __init__(
        self,
        config: PipelineConfig | None = None,
        loader: BaseLoader | None = None,
        embedding_model: EmbeddingPort | None = None,
    ):
        self._config = config or PipelineConfig()
        self._loader = loader or AutoLoader.default()
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
            yield from self._iter_embedded(self._process(lc))
        finally:
            lc.cleanup()

    async def astream_run(
        self, source: str | Path | SourceContent
    ) -> AsyncGenerator[Chunk, None]:
        lc = await self._aload(source)
        try:
            chunks = await asyncio.to_thread(self._process, lc)
            async for chunk in self._aiter_embedded(chunks):
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

    # ── Embedding ──────────────────────────────────────────────────────────
    #
    # 全部四个入口（run / arun / stream_run / astream_run）都走下面这两个
    # 窗口化实现，不再有第二种写法。此前 astream_run 是逐 chunk 串行
    # `await aembed()` —— 200 个 chunk 就是 200 次**依次排队**的往返，
    # 模型服务大部分时间在空转等网络。
    #
    # 窗口不等于"一个请求装多条"：窗口是**产出粒度**，装几条由
    # `embedding_batch` 按模型声明的 `batch_limit` 决定。不支持原生批量的
    # 模型（batch_limit=1）收益来自并发，支持的（OpenAI）则直接省下往返。

    def _require_model(self) -> EmbeddingPort:
        if self._embedding_model is None:
            raise RuntimeError(
                "Embedding model is not initialized, cannot execute embedding."
            )
        return self._embedding_model

    def _windows(self, chunks: list[Chunk]) -> Iterator[list[Chunk]]:
        size = self._config.embed_batch_size
        for start in range(0, len(chunks), size):
            yield chunks[start : start + size]

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

    async def _aiter_embedded(self, chunks: list[Chunk]) -> AsyncGenerator[Chunk, None]:
        if not (self._config.embed and self._embedding_model):
            for chunk in chunks:
                yield chunk
            return
        model = self._require_model()
        for window in self._windows(chunks):
            embeddings = await aembed_documents(
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

    async def _aembed_chunks(self, chunks: list[Chunk]) -> None:
        async for _ in self._aiter_embedded(chunks):
            pass
