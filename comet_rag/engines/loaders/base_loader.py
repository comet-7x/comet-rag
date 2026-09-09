import asyncio
import inspect
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, final

from comet_rag.engines.defaults import DEFAULT_LOADER_CONCURRENCY
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.ports.gate import GatedResource

#: 兼容旧名；数字与理由都在 `engines/defaults.py`
DEFAULT_MAX_CONCURRENCY = DEFAULT_LOADER_CONCURRENCY


class BaseLoader(GatedResource, ABC):
    """提供保守的批量回退；批内并发与进程级闸门是两个独立上限。"""

    @abstractmethod
    def _load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        """适配器真正执行同步加载的扩展点。"""

    @final
    def load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        """同步加载，并把实现专属选项转发给 `_load`。"""
        return self._through_gate_sync(lambda: self._load(source, **kwargs))

    @abstractmethod
    async def _aload(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        """异步加载扩展点；公开外壳不可覆写，避免绕过闸门。"""

    @final
    async def aload(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        """异步加载一个来源。受进程级闸门保护，不可覆写。`**kwargs` 同 `load`。"""
        return await self._through_gate(lambda: self._aload(source, **kwargs))

    @abstractmethod
    def cleanup(self) -> None: ...

    async def acleanup(self) -> None:
        """兼容旧 `aclose()`；否则在线程中执行同步清理。"""

        legacy_closer = getattr(self, "aclose", None)
        if callable(legacy_closer):
            result = legacy_closer()
            if inspect.isawaitable(result):
                await result
            return
        await asyncio.to_thread(self.cleanup)

    def _reject_unsupported(self, options: dict[str, Any]) -> None:
        """拒绝未知选项，避免拼写错误被静默忽略。"""
        if options:
            name = next(iter(options))
            raise TypeError(
                f"{type(self).__name__} got an unexpected keyword argument {name!r}"
            )

    @staticmethod
    def _validate_max_concurrency(max_concurrency: int) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency 必须大于 0，收到 {max_concurrency}")

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[LoaderContent]:
        """Load a batch with a bounded thread-pool fallback."""

        self._validate_max_concurrency(max_concurrency)
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(self.load, source) for source in sources]
            return [f.result() for f in futures]

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[LoaderContent]:
        """Load a batch through ``aload`` with bounded task concurrency."""

        self._validate_max_concurrency(max_concurrency)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _load(source: SourceContent | str) -> LoaderContent:
            async with semaphore:
                return await self.aload(source)

        return await asyncio.gather(*[_load(s) for s in sources])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cleanup()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.acleanup()
