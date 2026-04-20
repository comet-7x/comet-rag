import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from comet_rag.engines.loaders.source_content import SourceContent


class LoaderResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path = Field(..., description="Local file path (temp copy or original)")
    source: SourceContent = Field(
        ..., description="Original source — for traceability only"
    )
    is_temp: bool = Field(default=False, description="Whether path is a temp file")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def cleanup(self) -> None:
        if self.is_temp:
            self.path.unlink(missing_ok=True)


class BaseLoader(ABC):
    @abstractmethod
    def load(self, source: SourceContent | str, *args, **kwargs) -> LoaderResult: ...

    @abstractmethod
    async def aload(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderResult: ...

    @abstractmethod
    def cleanup(self) -> None: ...

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        **kwargs,
    ) -> list[LoaderResult]:
        max_concurrency = kwargs.pop("max_concurrency", 10)
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(self.load, s, **kwargs) for s in sources]
            return [f.result() for f in futures]

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        **kwargs,
    ) -> list[LoaderResult]:
        max_concurrency = kwargs.pop("max_concurrency", 10)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _load(source: SourceContent | str) -> LoaderResult:
            async with semaphore:
                return await self.aload(source, **kwargs)

        return list(await asyncio.gather(*[_load(s) for s in sources]))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cleanup()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.cleanup()
