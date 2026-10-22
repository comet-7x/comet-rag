from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, final
from urllib.parse import unquote, urlparse

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from comet_rag.engines.documents.formats import (
    ContentTypeMismatch as _ContentTypeMismatch,
)
from comet_rag.engines.offload import run_sync
from comet_rag.infrastructure.sources._lifecycle import AsyncActivity, SyncActivity
from comet_rag.infrastructure.sources.base import (
    DEFAULT_MAX_CONCURRENCY,
    AsyncBaseLoader,
    BaseLoader,
    finish,
    gather_loads,
)
from comet_rag.infrastructure.sources.file_info import (
    TemporaryFileRegistry,
    build_file_metadata,
    resolve_downloaded_extension,
)
from comet_rag.ports.source import LoadedResource, SourceContent

LoaderContent = LoadedResource

DEFAULT_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024

# Preserve the public import path used before the exception moved to data_type.py.
ContentTypeMismatch = _ContentTypeMismatch


class DownloadRequestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    headers: dict[str, str] | None = None
    content: bytes | None = None
    data: dict[str, Any] | None = None
    params: dict[str, str] | None = None
    timeout: int = 30
    follow_redirects: bool = True
    max_bytes: int | None = Field(
        default=None,
        gt=0,
        description="本次下载的字节上限；只能收紧 URLLoader 的全局上限",
    )


class DownloadTooLarge(ValueError):
    """远端响应超过应用层下载上限。"""


class _URLSettings:
    """只共享下载规则与临时文件登记，不持有网络客户端。"""

    def _configure(
        self,
        download_dir: str | Path | None = None,
        *,
        timeout: int = 30,
        follow_redirects: bool = True,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        redirect_validator: Callable[[str], None] | None = None,
        max_redirects: int = 20,
    ) -> None:
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be greater than zero")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self.download_dir = Path(download_dir) if download_dir else None
        self._temporary_files = TemporaryFileRegistry(self.download_dir)
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._max_download_bytes = max_download_bytes
        self._redirect_validator = redirect_validator
        self._max_redirects = max_redirects

    @property
    def temp_files(self) -> list[str]:
        return self._temporary_files.paths

    def _build_metadata(
        self,
        file_path: str,
        source: SourceContent,
        response_url: httpx.URL,
    ) -> dict[str, Any]:
        response_name = Path(unquote(urlparse(str(response_url)).path)).name
        source_name = Path(unquote(source.parsed_url.path)).name
        declared_name = response_name or source_name or Path(file_path).name
        return build_file_metadata(file_path, source, file_name=declared_name)

    def _effective_max_bytes(self, config: DownloadRequestConfig) -> int:
        if config.max_bytes is None:
            return self._max_download_bytes
        return min(config.max_bytes, self._max_download_bytes)

    @staticmethod
    def _check_content_length(response: httpx.Response, limit: int) -> None:
        raw = response.headers.get("content-length")
        if raw is None:
            return
        try:
            declared = int(raw)
        except ValueError:
            return  # 不可信的头不能放宽后面的累计字节检查
        if declared > limit:
            raise DownloadTooLarge(
                f"Remote response declares {declared} bytes, limit is {limit}"
            )

    def _new_temp_file(self):
        # 异步路径需要让文件跨多个 await 保持打开，不能使用词法 with 块。
        return self._temporary_files.create()

    def _discard_temp(self, path: str) -> None:
        self._temporary_files.discard(path)

    def _finalize_temp(self, path: str, response_url: httpx.URL) -> str:
        declared_name = Path(unquote(urlparse(str(response_url)).path)).name
        label = resolve_downloaded_extension(path, declared_name)
        return self._temporary_files.replace_suffix(path, label)

    def _cleanup_files(self) -> None:
        try:
            self._temporary_files.cleanup()
        except OSError as exc:
            logger.warning(f"临时文件清理失败，已保留登记以便重试: {exc!r}")


class URLLoader(_URLSettings, SyncActivity, BaseLoader):
    """同步 HTTP 下载；只关闭由本实例创建的客户端。"""

    def __init__(
        self,
        download_dir: str | Path | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        redirect_validator: Callable[[str], None] | None = None,
        max_redirects: int = 20,
    ) -> None:
        self._configure(
            download_dir,
            timeout=timeout,
            follow_redirects=follow_redirects,
            max_download_bytes=max_download_bytes,
            redirect_validator=redirect_validator,
            max_redirects=max_redirects,
        )
        self._client = client
        self._owns_client = client is None
        self._initialize_lifecycle()
        self._client_lock = threading.RLock()

    def _shared_client(self) -> httpx.Client:
        with self._client_lock:
            if self._client is None:
                self._client = httpx.Client(
                    timeout=self._timeout, follow_redirects=False
                )
            return self._client

    def _download(
        self,
        url: str,
        config: DownloadRequestConfig,
        client: httpx.Client | None = None,
    ) -> tuple[str, httpx.URL]:
        headers = config.headers or {
            "User-Agent": f"Mozilla/5.0 (compatible; comet-rag {type(self).__name__})"
        }

        def _do_request(c: httpx.Client) -> tuple[str, httpx.URL]:
            method = "POST" if (config.content or config.data) else "GET"
            request = c.build_request(
                method,
                url,
                headers=headers,
                content=config.content,
                data=config.data,
                params=config.params,
                timeout=config.timeout,
            )
            redirects = 0
            while True:
                response = c.send(request, stream=True, follow_redirects=False)
                if config.follow_redirects and response.is_redirect:
                    next_request = response.next_request
                    response.close()
                    if next_request is None:
                        raise httpx.RemoteProtocolError(
                            "Redirect response is missing a usable Location header",
                            request=request,
                        )
                    redirects += 1
                    if redirects > self._max_redirects:
                        raise httpx.TooManyRedirects(
                            f"Exceeded {self._max_redirects} redirects", request=request
                        )
                    if self._redirect_validator is not None:
                        self._redirect_validator(str(next_request.url))
                    request = next_request
                    continue
                try:
                    response.raise_for_status()
                    return self._stream_response(response, config), response.url
                finally:
                    response.close()

        return _do_request(client if client is not None else self._shared_client())

    def _stream_response(
        self, response: httpx.Response, config: DownloadRequestConfig
    ) -> str:
        limit = self._effective_max_bytes(config)
        self._check_content_length(response, limit)
        tmp = self._new_temp_file()
        total = 0
        try:
            with tmp:
                for chunk in response.iter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
                    total += len(chunk)
                    if total > limit:
                        raise DownloadTooLarge(
                            f"Remote response exceeded {limit} bytes while streaming"
                        )
                    tmp.write(chunk)
            if total == 0:
                raise ValueError(f"Downloaded empty content from {response.url}")
            return self._finalize_temp(tmp.name, response.url)
        except BaseException:
            self._discard_temp(tmp.name)
            raise

    def _load(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.Client | None = None,
        **unsupported: Any,
    ) -> LoaderContent:
        self._reject_unsupported(unsupported)
        with self._activity():
            return self._load_impl(
                source, download_config=download_config, client=client
            )

    def _load_impl(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.Client | None = None,
    ) -> LoaderContent:
        if isinstance(source, str):
            source = SourceContent(source)
        if not source.is_url:
            raise ValueError(f"URLLoader only handles URLs, got: {source.source!r}")
        config = download_config or DownloadRequestConfig(
            timeout=self._timeout, follow_redirects=self._follow_redirects
        )
        file_path, response_url = self._download(source.source, config, client)
        try:
            metadata = self._build_metadata(file_path, source, response_url)
        except BaseException:
            self._discard_temp(file_path)
            raise
        return LoaderContent(
            path=Path(file_path),
            source=source,
            is_temp=True,
            metadata=metadata,
            _release=lambda: self._temporary_files.release(file_path),
        )

    @final
    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        download_config: DownloadRequestConfig | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[LoaderContent]:
        self._validate_max_concurrency(max_concurrency)
        config = download_config or DownloadRequestConfig(
            timeout=self._timeout, follow_redirects=self._follow_redirects
        )
        # 整批只登记一次活动操作；worker 直接调用实现方法，避免嵌套登记
        # 在 cleanup 已等待时造成循环等待。
        with self._activity(), self._client_lock:
            client = self._shared_client()
            return self._batch_load_with(client, sources, config, max_concurrency)

    def _batch_load_with(
        self,
        client: httpx.Client,
        sources: list[SourceContent] | list[str],
        config: DownloadRequestConfig,
        max_concurrency: int,
    ) -> list[LoaderContent]:
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        def _gated(source: SourceContent | str) -> LoaderContent:
            # 直接调用实现可避免活动操作重复登记，但仍须单独经过闸门。
            return self._through_gate_sync(
                lambda: self._load_impl(source, download_config=config, client=client)
            )

        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(_gated, s) for s in sources]
            return [f.result() for f in futures]

    def cleanup(self) -> None:
        self._begin_cleanup()
        try:
            self._cleanup_files()
            with self._client_lock:
                if self._owns_client and self._client is not None:
                    self._client.close()
                    self._client = None
        finally:
            self._end_cleanup()


class AsyncURLLoader(_URLSettings, AsyncActivity, AsyncBaseLoader):
    """原生异步 HTTP 下载；文件工作完成后才能退出活动边界。"""

    def __init__(
        self,
        download_dir: str | Path | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        redirect_validator: Callable[[str], None] | None = None,
        max_redirects: int = 20,
    ) -> None:
        self._configure(
            download_dir,
            timeout=timeout,
            follow_redirects=follow_redirects,
            max_download_bytes=max_download_bytes,
            redirect_validator=redirect_validator,
            max_redirects=max_redirects,
        )
        self._client = client
        self._owns_client = client is None
        self._initialize_lifecycle()

    def _shared_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False
            )
        return self._client

    async def _download(
        self,
        url: str,
        config: DownloadRequestConfig,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[str, httpx.URL]:
        headers = config.headers or {
            "User-Agent": f"Mozilla/5.0 (compatible; comet-rag {type(self).__name__})"
        }

        async def _do_request(c: httpx.AsyncClient) -> tuple[str, httpx.URL]:
            method = "POST" if (config.content or config.data) else "GET"
            request = c.build_request(
                method,
                url,
                headers=headers,
                content=config.content,
                data=config.data,
                params=config.params,
                timeout=config.timeout,
            )
            redirects = 0
            while True:
                response = await c.send(request, stream=True, follow_redirects=False)
                if config.follow_redirects and response.is_redirect:
                    next_request = response.next_request
                    await response.aclose()
                    if next_request is None:
                        raise httpx.RemoteProtocolError(
                            "Redirect response is missing a usable Location header",
                            request=request,
                        )
                    redirects += 1
                    if redirects > self._max_redirects:
                        raise httpx.TooManyRedirects(
                            f"Exceeded {self._max_redirects} redirects", request=request
                        )
                    if self._redirect_validator is not None:
                        await run_sync(self._redirect_validator, str(next_request.url))
                    request = next_request
                    continue
                try:
                    response.raise_for_status()
                    return await self._stream_response(response, config), response.url
                finally:
                    await response.aclose()

        return await _do_request(
            client if client is not None else self._shared_client()
        )

    async def _stream_response(
        self, response: httpx.Response, config: DownloadRequestConfig
    ) -> str:
        limit = self._effective_max_bytes(config)
        self._check_content_length(response, limit)
        tmp = self._new_temp_file()
        path = tmp.name
        total = 0
        try:
            async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise DownloadTooLarge(
                        f"Remote response exceeded {limit} bytes while streaming"
                    )
                await run_sync(tmp.write, chunk)
            await run_sync(tmp.close)
            if total == 0:
                raise ValueError(f"Downloaded empty content from {response.url}")

            def finalize() -> str:
                nonlocal path
                path = self._finalize_temp(path, response.url)
                return path

            return await run_sync(finalize)
        except BaseException:

            def discard() -> None:
                tmp.close()
                self._discard_temp(path)

            await run_sync(discard)
            raise

    async def _load(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.AsyncClient | None = None,
        **unsupported: Any,
    ) -> LoaderContent:
        self._reject_unsupported(unsupported)
        async with self._activity():
            return await self._load_impl(
                source, download_config=download_config, client=client
            )

    async def _load_impl(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> LoaderContent:
        if isinstance(source, str):
            source = SourceContent(source)
        if not source.is_url:
            raise ValueError(f"URLLoader only handles URLs, got: {source.source!r}")
        config = download_config or DownloadRequestConfig(
            timeout=self._timeout, follow_redirects=self._follow_redirects
        )
        file_path, response_url = await self._download(source.source, config, client)
        try:
            metadata = await run_sync(
                self._build_metadata, file_path, source, response_url
            )
        except BaseException:
            await run_sync(self._discard_temp, file_path)
            raise
        return LoaderContent(
            path=Path(file_path),
            source=source,
            is_temp=True,
            metadata=metadata,
            _release=lambda: self._temporary_files.release(file_path),
        )

    @final
    async def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        download_config: DownloadRequestConfig | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[LoaderContent]:
        self._validate_max_concurrency(max_concurrency)
        config = download_config or DownloadRequestConfig(
            timeout=self._timeout, follow_redirects=self._follow_redirects
        )
        semaphore = asyncio.Semaphore(max_concurrency)

        # 整批只登记一次活动操作；worker 直接调用实现方法，避免嵌套登记。
        async with self._activity():
            client = self._shared_client()

            async def _load_one(source: SourceContent | str) -> LoaderContent:
                async with semaphore:  # 本次调用的宽度
                    # 与同步批量同理：绕开的是活动登记，不是限流。
                    return await self._through_gate(
                        lambda: self._load_impl(
                            source, download_config=config, client=client
                        )
                    )

            return await gather_loads([_load_one(s) for s in sources])

    async def cleanup(self) -> None:
        await finish(asyncio.create_task(self._cleanup()))

    async def _cleanup(self) -> None:
        await self._begin_cleanup()
        try:
            await run_sync(self._cleanup_files)
            if self._owns_client and self._client is not None:
                await self._client.aclose()
                self._client = None
        finally:
            self._end_cleanup()
