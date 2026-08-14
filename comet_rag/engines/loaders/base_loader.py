import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from comet_rag.engines.loaders.types import LoaderContent, SourceContent

DEFAULT_MAX_CONCURRENCY = 10


class BaseLoader(ABC):
    """Loader contract with conservative batch fallbacks.

    The default batch methods are suitable for loaders whose synchronous work can
    safely run in threads. Loaders with resource-specific requirements (connection
    pooling, rate limits, process pools, and so on) should override them.

    ``DEFAULT_MAX_CONCURRENCY`` is deliberately a safety cap rather than a claim
    about optimal throughput. Callers should tune it for their resource budget.
    """

    @abstractmethod
    def load(self, source: SourceContent | str, *args, **kwargs) -> LoaderContent: ...

    @abstractmethod
    async def aload(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderContent: ...

    @abstractmethod
    def cleanup(self) -> None: ...

    async def acleanup(self) -> None:
        """Release resources without blocking the event loop.

        The default delegates synchronous cleanup to a worker thread. Loaders that
        own native asynchronous resources should override this method.
        """

        await asyncio.to_thread(self.cleanup)

    @staticmethod
    def _validate_max_concurrency(max_concurrency: int) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency 必须大于 0，收到 {max_concurrency}")

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        **kwargs,
    ) -> list[LoaderContent]:
        """Load a batch with a bounded thread-pool fallback."""

        self._validate_max_concurrency(max_concurrency)
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(self.load, s, **kwargs) for s in sources]
            return [f.result() for f in futures]

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        **kwargs,
    ) -> list[LoaderContent]:
        """Load a batch through ``aload`` with bounded task concurrency."""

        self._validate_max_concurrency(max_concurrency)
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
        await self.acleanup()
