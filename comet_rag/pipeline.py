from __future__ import annotations

from typing import Self

from comet_rag.engines.documents.normalization import DocumentNormalizationStrategy
from comet_rag.engines.pipelines.hooks import HookProvider
from comet_rag.engines.pipelines.types import PipelineConfig
from comet_rag.infrastructure.sources import AutoLoader
from comet_rag.ports import EmbeddingPort, SourceLoaderPort
from comet_rag.services.pipeline import Pipeline as PipelineService


class Pipeline(PipelineService):
    """面向库用户的便捷入口；缺省 Loader 的装配只发生在这层公共门面。"""

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

    def cleanup(self) -> None:
        """只关闭本门面创建的默认 Loader；注入资源仍由调用方管理。"""
        if self._owns_loader:
            self._loader.cleanup()

    async def acleanup(self) -> None:
        if self._owns_loader:
            await self._loader.acleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.cleanup()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.acleanup()


__all__ = ["Pipeline", "PipelineConfig"]
