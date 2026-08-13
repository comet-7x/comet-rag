import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path

import httpx

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent, SourceType
from comet_rag.engines.loaders.url_loader import DownloadRequestConfig


class AutoLoader(BaseLoader):
    """Routes each source to the appropriate loader based on SourceType.

    Pass a custom `loaders` mapping to override defaults or register new
    source types (e.g. MinioLoader for a custom scheme).
    """

    def __init__(
        self,
        loaders: dict[SourceType, BaseLoader] | None = None,
        download_dir: str | Path | None = None,
        max_download_bytes: int | None = None,
        redirect_validator: Callable[[str], None] | None = None,
    ) -> None:
        if loaders is not None:
            self._loaders = loaders
        else:
            from comet_rag.engines.loaders.local_loader import LocalLoader
            from comet_rag.engines.loaders.url_loader import (
                DEFAULT_MAX_DOWNLOAD_BYTES,
                URLLoader,
            )

            self._loaders: dict[SourceType, BaseLoader] = {
                SourceType.LOCAL: LocalLoader(),
                SourceType.URL: URLLoader(
                    download_dir=download_dir,
                    max_download_bytes=(
                        max_download_bytes
                        if max_download_bytes is not None
                        else DEFAULT_MAX_DOWNLOAD_BYTES
                    ),
                    redirect_validator=redirect_validator,
                ),
            }

    def _resolve(self, source: SourceContent) -> BaseLoader:
        try:
            return self._loaders[source.pre_source_type]
        except KeyError as e:
            raise ValueError(
                f"No loader registered for source type {source.pre_source_type!r}: {source.source!r}"
            ) from e

    def load(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.Client | None = None,
        **kwargs,
    ) -> LoaderContent:
        if isinstance(source, str):
            source = SourceContent(source)
        return self._resolve(source).load(
            source, download_config=download_config, client=client, **kwargs
        )

    async def aload(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.AsyncClient | None = None,
        **kwargs,
    ) -> LoaderContent:
        if isinstance(source, str):
            source = SourceContent(source)
        # `_resolve()` 首次读取 `SourceContent.is_local` 时会执行一次 `stat()`。
        # 网络文件系统或大批量输入下不能把这次阻塞 I/O 放在事件循环上。
        loader = await asyncio.to_thread(self._resolve, source)
        return await loader.aload(
            source, download_config=download_config, client=client, **kwargs
        )

    def cleanup(self) -> None:
        for loader in self._loaders.values():
            loader.cleanup()

    async def aclose(self) -> None:
        for loader in self._loaders.values():
            closer = getattr(loader, "aclose", None)
            if callable(closer):
                result = closer()
                if inspect.isawaitable(result):
                    await result
            else:
                await asyncio.to_thread(loader.cleanup)
