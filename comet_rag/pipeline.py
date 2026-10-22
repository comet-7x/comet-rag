from __future__ import annotations

from typing import Self

from comet_rag.engines.documents.normalization import DocumentNormalizationStrategy
from comet_rag.engines.pipelines.hooks import AsyncHookProvider, HookProvider
from comet_rag.engines.pipelines.types import PipelineConfig
from comet_rag.infrastructure.sources import AsyncAutoLoader, AutoLoader
from comet_rag.ports import (
    AsyncEmbeddingPort,
    AsyncSourceLoaderPort,
    EmbeddingPort,
    SourceLoaderPort,
)
from comet_rag.services.pipeline import AsyncPipeline as AsyncPipelineService
from comet_rag.services.pipeline import Pipeline as PipelineService


class Pipeline(PipelineService):
    def __init__(
        self,
        config: PipelineConfig | None = None,
        loader: SourceLoaderPort | None = None,
        embedding_model: EmbeddingPort | None = None,
        hooks: HookProvider | None = None,
        normalizer: DocumentNormalizationStrategy | None = None,
    ) -> None:
        selected_loader = loader or AutoLoader.default()
        self._owns_loader = loader is None
        super().__init__(
            config=config,
            loader=selected_loader,
            embedding_model=embedding_model,
            hooks=hooks,
            normalizer=normalizer,
        )

    def close(self) -> None:
        """只关闭本门面创建的默认 Loader；注入资源仍由调用方管理。"""
        if self._owns_loader:
            self._loader.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncPipeline(AsyncPipelineService):
    def __init__(
        self,
        config: PipelineConfig | None = None,
        loader: AsyncSourceLoaderPort | None = None,
        embedding_model: AsyncEmbeddingPort | None = None,
        hooks: AsyncHookProvider | None = None,
        normalizer: DocumentNormalizationStrategy | None = None,
    ) -> None:
        selected_loader = loader or AsyncAutoLoader.default()
        self._owns_loader = loader is None
        super().__init__(
            config=config,
            loader=selected_loader,
            embedding_model=embedding_model,
            hooks=hooks,
            normalizer=normalizer,
        )

    async def close(self) -> None:
        if self._owns_loader:
            await self._loader.cleanup()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


__all__ = ["Pipeline", "AsyncPipeline", "PipelineConfig"]
