import asyncio
import io
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.data_type import AllowExt, ParseConfig
from comet_rag.engines.loaders.types import LoaderContent, SourceContent, SourceType
from comet_rag.engines.utils.file_detector import detect_content_type_from_stream


class DownloadRequestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    headers: dict[str, str] | None = None
    content: bytes | None = None
    data: dict[str, Any] | None = None
    params: dict[str, str] | None = None
    timeout: int = 30
    follow_redirects: bool = True


class URLLoader(BaseLoader):
    """从 URL 下载到本地临时文件。

    **连接复用（spec S4-4）**：httpx client 由实例持有并跨调用复用，
    而不是每次下载现建现销 —— 后者每次都要重建连接池并重新 TLS 握手，
    批量入库大量 URL 时是纯浪费。

    client 有三种来源，优先级由高到低：
      1. 单次调用传入的 `client=` —— 只在这一次生效（`batch_load` 用它共享连接）
      2. 构造时注入的 client —— 由调用方负责关闭，本类不碰
      3. 本类懒创建的 client —— 由本类在 `cleanup()` / `aclose()` 时关闭

    第 2 与第 3 的区别很重要：关掉别人注入的 client 会连带影响调用方
    其余还在用它的代码。参照 `Qwen3VLEmbeddingModel` 的同款处理。
    """

    def __init__(
        self,
        download_dir: str | Path | None = None,
        *,
        client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
    ) -> None:
        self.download_dir = Path(download_dir) if download_dir else None
        self._temp_files: list[str] = []
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._client = client
        self._async_client = async_client
        # 只关自己造的
        self._owns_client = client is None
        self._owns_async_client = async_client is None

    @property
    def temp_files(self) -> list[str]:
        return self._temp_files.copy()

    def _shared_client(self) -> httpx.Client:
        """懒创建：只用异步路径的调用方不必平白多一个同步连接池。"""
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout, follow_redirects=self._follow_redirects
            )
        return self._client

    def _shared_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=self._follow_redirects
            )
        return self._async_client

    def _build_metadata(self, file_path: str, source: SourceContent) -> dict[str, Any]:
        path = Path(file_path)
        file_type = path.suffix.lstrip(".").lower()
        file_size = path.stat().st_size
        if file_size <= 0:
            raise ValueError(
                f"File is empty and cannot be loaded. Path: {path.resolve()}, type: {file_type}"
            )
        metadata = {
            "source_type": source.pre_source_type,
            "file_name": path.name,
            "file_type": file_type,
            "file_size": file_size,
        }
        try:
            metadata["parse_config"] = ParseConfig.from_extension(file_type)
        except ValueError:
            metadata["parse_config"] = None

        return metadata

    def _download(
        self,
        url: str,
        config: DownloadRequestConfig,
        client: httpx.Client | None = None,
    ) -> str:
        headers = config.headers or {
            "User-Agent": f"Mozilla/5.0 (compatible; comet-rag {type(self).__name__})"
        }

        def _do_request(c: httpx.Client) -> bytes:
            method = "POST" if (config.content or config.data) else "GET"
            response = c.request(
                method,
                url,
                headers=headers,
                content=config.content,
                data=config.data,
                params=config.params,
                timeout=config.timeout,
                follow_redirects=config.follow_redirects,
            )
            response.raise_for_status()
            return response.content

        try:
            raw = _do_request(client if client is not None else self._shared_client())
        except Exception as e:
            raise ValueError(f"Download failed from {url}: {e!s}") from e

        if not raw:
            raise ValueError(f"Downloaded empty content from {url}")

        url_ext = Path(urlparse(url).path).suffix.lstrip(".").lower()
        label = (
            url_ext
            if url_ext in AllowExt._value2member_map_
            else detect_content_type_from_stream(io.BytesIO(raw))
        )
        with tempfile.NamedTemporaryFile(
            suffix=f".{label}", delete=False, dir=self.download_dir
        ) as tmp:
            self._temp_files.append(tmp.name)
            tmp.write(raw)
            return tmp.name

    async def _adownload(
        self,
        url: str,
        config: DownloadRequestConfig,
        client: httpx.AsyncClient | None = None,
    ) -> str:
        headers = config.headers or {
            "User-Agent": f"Mozilla/5.0 (compatible; comet-rag {type(self).__name__})"
        }

        async def _do_request(c: httpx.AsyncClient) -> bytes:
            method = "POST" if (config.content or config.data) else "GET"
            response = await c.request(
                method,
                url,
                headers=headers,
                content=config.content,
                data=config.data,
                params=config.params,
                timeout=config.timeout,
                follow_redirects=config.follow_redirects,
            )
            response.raise_for_status()
            return response.content

        try:
            raw = await _do_request(
                client if client is not None else self._shared_async_client()
            )
        except Exception as e:
            raise ValueError(f"Async download failed from {url}: {e!s}") from e

        if not raw:
            raise ValueError(f"Downloaded empty content from {url}")

        url_ext = Path(urlparse(url).path).suffix.lstrip(".").lower()
        loop = asyncio.get_running_loop()
        if url_ext in AllowExt._value2member_map_:
            label = url_ext
        else:
            label = await loop.run_in_executor(
                None, lambda: detect_content_type_from_stream(io.BytesIO(raw))
            )

        def _save() -> str:
            with tempfile.NamedTemporaryFile(
                suffix=f".{label}", delete=False, dir=self.download_dir
            ) as tmp:
                self._temp_files.append(tmp.name)
                tmp.write(raw)
                return tmp.name

        return await loop.run_in_executor(None, _save)

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
        if source.pre_source_type != SourceType.URL:
            raise ValueError(f"URLLoader only handles URLs, got: {source.source!r}")
        config = download_config or DownloadRequestConfig()
        file_path = self._download(source.source, config, client)
        return LoaderContent(
            path=Path(file_path),
            source=source,
            is_temp=True,
            metadata=self._build_metadata(file_path, source),
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
        if source.pre_source_type != SourceType.URL:
            raise ValueError(f"URLLoader only handles URLs, got: {source.source!r}")
        config = download_config or DownloadRequestConfig()
        file_path = await self._adownload(source.source, config, client)
        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(
            None, self._build_metadata, file_path, source
        )
        return LoaderContent(
            path=Path(file_path), source=source, is_temp=True, metadata=metadata
        )

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        download_config: DownloadRequestConfig | None = None,
        max_concurrency: int = 10,
        **kwargs,
    ) -> list[LoaderContent]:
        from concurrent.futures import ThreadPoolExecutor

        config = download_config or DownloadRequestConfig()
        client = self._shared_client()
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [
                executor.submit(
                    self.load, s, download_config=config, client=client, **kwargs
                )
                for s in sources
            ]
            return [f.result() for f in futures]

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        download_config: DownloadRequestConfig | None = None,
        max_concurrency: int = 10,
        **kwargs,
    ) -> list[LoaderContent]:
        config = download_config or DownloadRequestConfig()
        semaphore = asyncio.Semaphore(max_concurrency)
        client = self._shared_async_client()

        async def _load(source: SourceContent | str) -> LoaderContent:
            async with semaphore:
                return await self.aload(
                    source, download_config=config, client=client, **kwargs
                )

        return await asyncio.gather(*[_load(s) for s in sources])

    def cleanup(self) -> None:
        """删除临时文件并关闭**自建**的同步 client。

        注入进来的 client 一律不碰 —— 调用方其余代码可能还在用它。
        异步 client 无法在同步上下文里正确关闭，请用 `aclose()`。
        """
        for path in self._temp_files:
            Path(path).unlink(missing_ok=True)
        self._temp_files.clear()
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    async def acleanup(self) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """异步收尾：临时文件 + 两个自建 client 都关掉。"""
        await asyncio.to_thread(self.cleanup)
        if self._owns_async_client and self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    async def __aexit__(self, *_):
        await self.aclose()
