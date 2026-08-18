import asyncio
import inspect
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import final

from comet_rag.engines.defaults import DEFAULT_LOADER_CONCURRENCY
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.ports.gate import GatedResource

#: 兼容旧名；数字与理由都在 `engines/defaults.py`
DEFAULT_MAX_CONCURRENCY = DEFAULT_LOADER_CONCURRENCY


class BaseLoader(GatedResource, ABC):
    """Loader contract with conservative batch fallbacks.

    The default batch methods are suitable for loaders whose synchronous work can
    safely run in threads. Loaders with resource-specific requirements (connection
    pooling, rate limits, process pools, and so on) should override them.

    批量方法的并发默认值来自 ``engines/defaults.py`` —— 它护的是本机文件描述符
    与对外连接数，跟模型侧的扇出不是同一种资源，所以是两个数字。跑参考服务时
    真正生效的值由 ``LimitsConfig`` 提供。

    ## 两个旋钮叠加

    ``max_concurrency`` 限的是**一次批量调用内**的宽度；进程级总量由闸门管
    （``bind_gate``，组合根挂上）。只有前者时，多个任务各自守规矩、加起来不
    守 —— 这正是模型侧实测出"配置写 4、实际 128"的那个失效模式，加载侧同样
    存在：`ingestion.py` 每个任务调一次 ``aload``，32 个任务就是 32 路抓取。

    ``load`` 与 ``aload`` 共用同一份闸门预算（#44 修复后）：闸门的计数换成了
    线程原语，两侧都能拿。
    """

    @abstractmethod
    def _load(self, source: SourceContent | str) -> LoaderContent:
        """适配器真正执行同步加载的扩展点。"""

    @final
    def load(self, source: SourceContent | str) -> LoaderContent:
        """同步加载一个来源。与 `aload` 共用同一份闸门预算。"""
        return self._through_gate_sync(lambda: self._load(source))

    @abstractmethod
    async def _aload(self, source: SourceContent | str) -> LoaderContent:
        """适配器真正执行异步加载的扩展点。

        扩展点是 `_aload` 而不是 `aload`：闸门必须在**每一次真实抓取**外面，
        如果子类能覆写 `aload`，它一覆写就把闸门覆写掉了 —— 而且不报错。
        拆成"final 的外壳 + abstract 的内核"，子类在类型层面就没有绕过的写法。
        """

    @final
    async def aload(self, source: SourceContent | str) -> LoaderContent:
        """异步加载一个来源。受进程级闸门保护，不可覆写。"""
        return await self._through_gate(lambda: self._aload(source))

    @abstractmethod
    def cleanup(self) -> None: ...

    async def acleanup(self) -> None:
        """Release resources without blocking the event loop.

        Legacy custom loaders may still expose ``aclose()`` from the previous
        duck-typed contract; honor it during the migration. Otherwise delegate
        synchronous cleanup to a worker thread. Loaders that own native asynchronous
        resources should override this method.
        """

        legacy_closer = getattr(self, "aclose", None)
        if callable(legacy_closer):
            result = legacy_closer()
            if inspect.isawaitable(result):
                await result
            return
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
