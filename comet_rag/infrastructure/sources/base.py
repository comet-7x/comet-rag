from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Self, final

from comet_rag.engines.defaults import DEFAULT_LOADER_CONCURRENCY
from comet_rag.ports.gate import GatedResource
from comet_rag.ports.source import LoadedResource, SourceContent

LoaderContent = LoadedResource
DEFAULT_MAX_CONCURRENCY = DEFAULT_LOADER_CONCURRENCY


async def finish[T](task: asyncio.Task[T]) -> T:
    """排干不可中断的收尾；调用方重复取消也不能提前释放资源。"""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            break
    if cancellation is not None:
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()


async def gather_loads[T](operations: list[Awaitable[T]]) -> list[T]:
    """失败时取消并排干同批操作，调用方离开活动边界后不再有文件写入。"""
    tasks = [asyncio.ensure_future(operation) for operation in operations]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()

        async def drain() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)

        await finish(asyncio.create_task(drain()))
        raise


class _LoaderOptions:
    def _reject_unsupported(self, options: dict[str, Any]) -> None:
        if options:
            name = next(iter(options))
            raise TypeError(
                f"{type(self).__name__} got an unexpected keyword argument {name!r}"
            )

    @staticmethod
    def _validate_max_concurrency(max_concurrency: int) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency 必须大于 0，收到 {max_concurrency}")


class BaseLoader(_LoaderOptions, GatedResource, ABC):
    """同步加载器；批内线程数与进程级闸门分别约束并发。"""

    @abstractmethod
    def _load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent: ...

    @final
    def load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        return self._through_gate_sync(lambda: self._load(source, **kwargs))

    @abstractmethod
    def cleanup(self) -> None: ...

    def close(self) -> None:
        self.cleanup()

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[LoaderContent]:
        self._validate_max_concurrency(max_concurrency)
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(self.load, source) for source in sources]
            return [future.result() for future in futures]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncBaseLoader(_LoaderOptions, GatedResource, ABC):
    """异步加载器；公开模板方法保证单次调用经过同一份资源预算。"""

    @abstractmethod
    async def _load(
        self, source: SourceContent | str, **kwargs: Any
    ) -> LoaderContent: ...

    @final
    async def load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        return await self._through_gate(lambda: self._load(source, **kwargs))

    @abstractmethod
    async def cleanup(self) -> None: ...

    async def close(self) -> None:
        await self.cleanup()

    async def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[LoaderContent]:
        self._validate_max_concurrency(max_concurrency)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def load_one(source: SourceContent | str) -> LoaderContent:
            async with semaphore:
                return await self.load(source)

        return await gather_loads([load_one(source) for source in sources])

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
