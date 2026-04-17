from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from comet_rag.engines.loaders.data_type import ParseConfig
from comet_rag.engines.loaders.source_content import SourceContent


class LoaderResult(BaseModel):
    content: str | bytes = Field(..., description="Loaded content")

    source: str = Field(..., description="Original source")
    source_id: str = Field(..., description="Unique identifier")

    extension: str | None = Field(None, description="File extension")
    parse_config: ParseConfig | None = Field(None, description="Parse config")

    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseLoader(ABC):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @classmethod
    def supports(cls, source: SourceContent) -> bool:
        return False

    @abstractmethod
    def load(self, source: SourceContent, **kwargs: Any) -> LoaderResult: ...

    @abstractmethod
    async def aload(self, source: SourceContent, **kwargs: Any) -> LoaderResult: ...

    def batch_load(
        self, sources: list[SourceContent], *, max_concurrency: int = 10, **kwargs: Any
    ) -> list[LoaderResult]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(self.load, s, **kwargs) for s in sources]
            return [future.result() for future in futures]

    async def abatch_load(
        self, sources: list[SourceContent], *, max_concurrency: int = 10, **kwargs: Any
    ) -> list[LoaderResult]:
        import asyncio

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _load_with_limit(source: SourceContent) -> LoaderResult:
            async with semaphore:
                return await self.aload(source, **kwargs)

        tasks = [_load_with_limit(s) for s in sources]
        return await asyncio.gather(*tasks)


# TODO 后续添加自动选择加载器
# def auto_loader(source: str):
#     for loader in ALL_LOADERS:
#         if loader.supports(source):
#             return loader()
