import asyncio
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from comet_rag.engines.loaders.base_loader import DEFAULT_MAX_CONCURRENCY, BaseLoader
from comet_rag.engines.loaders.data_type import (
    ContentTypeMismatch as _ContentTypeMismatch,
)
from comet_rag.engines.loaders.data_type import (
    ParseConfig,
    is_allowed_extension,
    resolve_detected_extension,
)
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.utils.file_detector import detect_content_type_from_path

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
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        redirect_validator: Callable[[str], None] | None = None,
        max_redirects: int = 20,
    ) -> None:
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be greater than zero")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self.download_dir = Path(download_dir) if download_dir else None
        self._temp_files: list[str] = []
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._max_download_bytes = max_download_bytes
        self._redirect_validator = redirect_validator
        self._max_redirects = max_redirects
        self._client = client
        self._async_client = async_client
        # 只关自己造的
        self._owns_client = client is None
        self._owns_async_client = async_client is None
        #: 保护"共享 client 正在被用"与"关掉它"这两件事互斥（PR 评审 #11）。
        #: `batch_load` 改用共享 client 之后（S4-4 要求复用连接池），
        #: 另一处调 `cleanup()` 就可能把它脚下的连接池抽掉，
        #: 在途请求会撞上"client has been closed"。
        self._client_lock = threading.Lock()
        self._lifecycle = threading.Condition()
        self._active_loads = 0
        self._cleanup_in_progress = False

    @property
    def temp_files(self) -> list[str]:
        return self._temp_files.copy()

    def _shared_client(self) -> httpx.Client:
        """懒创建：只用异步路径的调用方不必平白多一个同步连接池。"""
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, follow_redirects=False)
        return self._client

    def _shared_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False
            )
        return self._async_client

    @contextmanager
    def _sync_activity(self) -> Iterator[None]:
        """Block cleanup while one synchronous load operation is active."""

        self._begin_activity()
        try:
            yield
        finally:
            self._end_activity()

    @asynccontextmanager
    async def _async_activity(self) -> AsyncIterator[None]:
        """Register async work without blocking its event loop during cleanup."""

        await self._abegin_activity()
        try:
            yield
        finally:
            self._end_activity()

    async def _abegin_activity(self) -> None:
        """Wait for cleanup with one worker dispatch and cancellation safety."""

        begin_task = asyncio.create_task(asyncio.to_thread(self._begin_activity))
        try:
            await asyncio.shield(begin_task)
        except asyncio.CancelledError:
            # `to_thread` cannot be cancelled after dispatch. Let it finish and
            # undo its registration so a cancelled waiter cannot leak an active
            # count and deadlock the next cleanup.
            await begin_task
            self._end_activity()
            raise

    def _begin_activity(self) -> None:
        with self._lifecycle:
            while self._cleanup_in_progress:
                self._lifecycle.wait()
            self._active_loads += 1

    def _end_activity(self) -> None:
        with self._lifecycle:
            self._active_loads -= 1
            if self._active_loads == 0:
                self._lifecycle.notify_all()

    def _begin_cleanup(self) -> None:
        with self._lifecycle:
            while self._cleanup_in_progress:
                self._lifecycle.wait()
            self._cleanup_in_progress = True
            while self._active_loads:
                self._lifecycle.wait()

    def _end_cleanup(self) -> None:
        with self._lifecycle:
            self._cleanup_in_progress = False
            self._lifecycle.notify_all()

    def _build_metadata(self, file_path: str, source: SourceContent) -> dict[str, Any]:
        path = Path(file_path)
        file_type = path.suffix.lstrip(".").lower()
        file_size = path.stat().st_size
        if file_size <= 0:
            raise ValueError(
                f"File is empty and cannot be loaded. Path: {path.resolve()}, type: {file_type}"
            )
        metadata = {
            "source_type": source.source_type,
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

        def _do_request(c: httpx.Client) -> str:
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
                    return self._stream_response(response, config)
                finally:
                    response.close()

        return _do_request(client if client is not None else self._shared_client())

    async def _adownload(
        self,
        url: str,
        config: DownloadRequestConfig,
        client: httpx.AsyncClient | None = None,
    ) -> str:
        headers = config.headers or {
            "User-Agent": f"Mozilla/5.0 (compatible; comet-rag {type(self).__name__})"
        }

        async def _do_request(c: httpx.AsyncClient) -> str:
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
                        await asyncio.to_thread(
                            self._redirect_validator, str(next_request.url)
                        )
                    request = next_request
                    continue
                try:
                    response.raise_for_status()
                    return await self._astream_response(response, config)
                finally:
                    await response.aclose()

        return await _do_request(
            client if client is not None else self._shared_async_client()
        )

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
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            delete=False, dir=self.download_dir
        )
        self._temp_files.append(tmp.name)
        return tmp

    def _discard_temp(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)
        with suppress(ValueError):
            self._temp_files.remove(path)

    def _finalize_temp(self, path: str, response_url: httpx.URL) -> str:
        source_ext = Path(urlparse(str(response_url)).path).suffix.lstrip(".").lower()
        source_allowed = is_allowed_extension(source_ext)
        try:
            detected = detect_content_type_from_path(path).lower().lstrip(".")
        except Exception as exc:  # 探测器不可用时才允许退回 URL 后缀
            if not source_allowed:
                raise ValueError(
                    f"Unable to determine downloaded content type for {response_url}"
                ) from exc
            logger.warning(f"内容探测失败，回退到 URL 后缀 {source_ext!r}: {exc!r}")
            label = source_ext
        else:
            label = resolve_detected_extension(source_ext, detected)
        target = str(Path(path).with_suffix(f".{label}"))
        Path(path).replace(target)
        self._temp_files[self._temp_files.index(path)] = target
        return target

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

    async def _astream_response(
        self, response: httpx.Response, config: DownloadRequestConfig
    ) -> str:
        limit = self._effective_max_bytes(config)
        self._check_content_length(response, limit)
        tmp = self._new_temp_file()
        total = 0
        try:
            async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise DownloadTooLarge(
                        f"Remote response exceeded {limit} bytes while streaming"
                    )
                await asyncio.to_thread(tmp.write, chunk)
            await asyncio.to_thread(tmp.close)
            if total == 0:
                raise ValueError(f"Downloaded empty content from {response.url}")
            return await asyncio.to_thread(self._finalize_temp, tmp.name, response.url)
        except BaseException:
            await asyncio.to_thread(tmp.close)
            await asyncio.to_thread(self._discard_temp, tmp.name)
            raise

    def _load(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.Client | None = None,
    ) -> LoaderContent:
        with self._sync_activity():
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
        file_path = self._download(source.source, config, client)
        return LoaderContent(
            path=Path(file_path),
            source=source,
            is_temp=True,
            metadata=self._build_metadata(file_path, source),
        )

    async def _aload(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> LoaderContent:
        async with self._async_activity():
            return await self._aload_impl(
                source, download_config=download_config, client=client
            )

    async def _aload_impl(
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
        with self._sync_activity(), self._client_lock:
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

        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [
                executor.submit(
                    self._load_impl, s, download_config=config, client=client
                )
                for s in sources
            ]
            return [f.result() for f in futures]

    async def abatch_load(
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
        async with self._async_activity():
            client = self._shared_async_client()

            async def _load_one(source: SourceContent | str) -> LoaderContent:
                async with semaphore:
                    return await self._aload_impl(
                        source, download_config=config, client=client
                    )

            return await asyncio.gather(*[_load_one(s) for s in sources])

    def cleanup(self) -> None:
        """删除临时文件并关闭**自建**的同步 client。

        注入进来的 client 一律不碰 —— 调用方其余代码可能还在用它。
        异步 client 无法在同步上下文里正确关闭，请用 `acleanup()`。
        """
        self._begin_cleanup()
        try:
            self._cleanup_sync_resources()
        finally:
            self._end_cleanup()

    def _cleanup_sync_resources(self) -> None:
        for path in self._temp_files:
            Path(path).unlink(missing_ok=True)
        self._temp_files.clear()
        with self._client_lock:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None

    async def acleanup(self) -> None:
        """异步收尾：临时文件 + 两个自建 client 都关掉。"""
        cleanup_task = asyncio.create_task(self._acleanup_impl())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # `to_thread` 无法停止已经开始的同步清理；等它完成再传播取消，
            # 否则会提前放开生命周期边界，让新下载撞上仍在删除的资源。
            await cleanup_task
            raise

    async def _acleanup_impl(self) -> None:
        await asyncio.to_thread(self._begin_cleanup)
        try:
            await asyncio.to_thread(self._cleanup_sync_resources)
            if self._owns_async_client and self._async_client is not None:
                await self._async_client.aclose()
                self._async_client = None
        finally:
            self._end_cleanup()

    async def aclose(self) -> None:
        """Backward-compatible alias for ``acleanup``."""

        await self.acleanup()
