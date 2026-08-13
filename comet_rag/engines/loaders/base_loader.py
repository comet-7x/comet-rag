import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from comet_rag.engines.loaders.types import LoaderContent, SourceContent


class BaseLoader(ABC):
    @abstractmethod
    def load(self, source: SourceContent | str, *args, **kwargs) -> LoaderContent: ...

    @abstractmethod
    async def aload(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderContent: ...

    @abstractmethod
    def cleanup(self) -> None: ...

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        **kwargs,
    ) -> list[LoaderContent]:
        max_concurrency = kwargs.pop("max_concurrency", 10)
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(self.load, s, **kwargs) for s in sources]
            return [f.result() for f in futures]

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        **kwargs,
    ) -> list[LoaderContent]:
        max_concurrency = kwargs.pop("max_concurrency", 10)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _load(source: SourceContent | str) -> LoaderContent:
            async with semaphore:
                return await self.aload(source, **kwargs)

        return await asyncio.gather(*[_load(s) for s in sources])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cleanup()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.cleanup()
