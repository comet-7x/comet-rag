"""S3-compatible object-storage loader.

The SDK dependency deliberately lives in ``infrastructure``.  ``engines`` only
defines the loader and routing contracts, so installing the core library does not
pull in an object-storage client.
"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.data_type import ParseConfig, is_allowed_extension
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.utils.file_detector import detect_content_type_from_path

DEFAULT_MAX_OBJECT_BYTES = 100 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_OBJECT_SCHEMES = frozenset({"s3", "minio"})
_GENERIC_TEXT_EXTENSIONS = frozenset(
    {"txt", "md", "py", "ts", "js", "java", "c", "cpp", "go", "php", "r", "rust"}
)


class ObjectTooLarge(ValueError):
    """The object exceeds the configured application-level size limit."""


class ObjectContentTypeMismatch(ValueError):
    """The key extension conflicts with the downloaded object content."""


@dataclass(frozen=True, slots=True)
class S3ObjectLocation:
    scheme: str
    bucket: str
    key: str


def parse_s3_uri(source: str) -> S3ObjectLocation:
    """Parse ``s3://bucket/key`` and ``minio://bucket/key`` sources."""

    parsed = urlparse(source)
    scheme = parsed.scheme.lower()
    if scheme not in _OBJECT_SCHEMES:
        raise ValueError(f"S3Loader only handles s3/minio URIs, got: {source!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("S3 URI must not contain credentials")
    try:
        if parsed.port is not None:
            raise ValueError("S3 URI must not contain a port")
    except ValueError as exc:
        raise ValueError(f"Invalid S3 bucket authority: {parsed.netloc!r}") from exc
    bucket = parsed.hostname or ""
    if not bucket:
        raise ValueError(f"S3 URI is missing a bucket: {source!r}")
    if parsed.query or parsed.fragment:
        raise ValueError("S3 URI query strings and fragments are not supported")
    key = unquote(parsed.path.lstrip("/"))
    if not key or not PurePosixPath(key).name:
        raise ValueError(f"S3 URI is missing an object key: {source!r}")
    return S3ObjectLocation(scheme=scheme, bucket=bucket, key=key)


class S3Loader(BaseLoader):
    """Download S3-compatible objects to managed local temporary files.

    Both the synchronous boto3 client and asynchronous aioboto3 client are lazy and
    reused across calls. Injected clients remain owned by the caller. Clients created
    by this loader are closed by ``cleanup()`` / ``acleanup()``.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        region_name: str = "us-east-1",
        addressing_style: str = "path",
        verify_ssl: bool = True,
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
        download_dir: str | Path | None = None,
        client: Any = None,
        async_client: Any = None,
    ) -> None:
        if max_object_bytes <= 0:
            raise ValueError("max_object_bytes must be greater than zero")
        if addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError("addressing_style must be one of: auto, path, virtual")
        if (access_key_id is None) != (secret_access_key is None):
            raise ValueError(
                "access_key_id and secret_access_key must be configured together"
            )
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._region_name = region_name
        self._addressing_style = addressing_style
        self._verify_ssl = verify_ssl
        self._max_object_bytes = max_object_bytes
        self._download_dir = Path(download_dir) if download_dir else None
        self._client = client
        self._async_client = async_client
        self._owns_client = client is None
        self._owns_async_client = async_client is None
        self._async_client_context: Any = None
        self._sync_client_lock = threading.Lock()
        self._async_client_lock = asyncio.Lock()
        self._temp_files_lock = threading.Lock()
        self._temp_files: list[str] = []

        # A single counter spans sync and async work. This matters because a service
        # shutdown can call async cleanup while a sync batch is still in a worker.
        self._lifecycle = threading.Condition()
        self._active_loads = 0
        self._cleanup_in_progress = False
        self._async_lifecycle_waiters: set[asyncio.Future[None]] = set()

    @property
    def temp_files(self) -> list[str]:
        with self._temp_files_lock:
            return self._temp_files.copy()

    def _client_kwargs(self) -> dict[str, Any]:
        from botocore.config import Config  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "region_name": self._region_name,
            "verify": self._verify_ssl,
            "config": Config(s3={"addressing_style": self._addressing_style}),
        }
        if self._endpoint_url is not None:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._access_key_id is not None:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        if self._session_token is not None:
            kwargs["aws_session_token"] = self._session_token
        return kwargs

    def _new_sync_client(self) -> Any:
        import boto3  # noqa: PLC0415

        return boto3.client("s3", **self._client_kwargs())

    def _new_async_client_context(self) -> Any:
        import aioboto3  # noqa: PLC0415

        return aioboto3.Session().client("s3", **self._client_kwargs())

    def _shared_client(self) -> Any:
        with self._sync_client_lock:
            if self._client is None:
                self._client = self._new_sync_client()
            return self._client

    async def _shared_async_client(self) -> Any:
        async with self._async_client_lock:
            if self._async_client is None:
                context = self._new_async_client_context()
                try:
                    client = await context.__aenter__()
                except BaseException:
                    with suppress(Exception):
                        await context.__aexit__(None, None, None)
                    raise
                self._async_client_context = context
                self._async_client = client
            return self._async_client

    @contextmanager
    def _sync_activity(self) -> Iterator[None]:
        self._begin_activity()
        try:
            yield
        finally:
            self._end_activity()

    @asynccontextmanager
    async def _async_activity(self) -> AsyncIterator[None]:
        await self._abegin_activity()
        try:
            yield
        finally:
            self._end_activity()

    async def _abegin_activity(self) -> None:
        """Register async activity without occupying a worker while cleanup runs."""

        while True:
            with self._lifecycle:
                if not self._cleanup_in_progress:
                    self._active_loads += 1
                    return
                waiter = self._new_async_lifecycle_waiter_locked()
            await self._await_async_lifecycle_change(waiter)

    def _new_async_lifecycle_waiter_locked(self) -> asyncio.Future[None]:
        waiter = asyncio.get_running_loop().create_future()
        self._async_lifecycle_waiters.add(waiter)
        return waiter

    async def _await_async_lifecycle_change(self, waiter: asyncio.Future[None]) -> None:
        try:
            await waiter
        finally:
            with self._lifecycle:
                self._async_lifecycle_waiters.discard(waiter)

    @staticmethod
    def _resolve_async_lifecycle_waiter(waiter: asyncio.Future[None]) -> None:
        if not waiter.done():
            waiter.set_result(None)

    def _notify_async_lifecycle_waiters_locked(self) -> None:
        waiters = tuple(self._async_lifecycle_waiters)
        self._async_lifecycle_waiters.clear()
        for waiter in waiters:
            with suppress(RuntimeError):
                waiter.get_loop().call_soon_threadsafe(
                    self._resolve_async_lifecycle_waiter,
                    waiter,
                )

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
                self._notify_async_lifecycle_waiters_locked()

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
            self._notify_async_lifecycle_waiters_locked()

    async def _abegin_cleanup(self) -> None:
        owns_cleanup = False
        try:
            while not owns_cleanup:
                with self._lifecycle:
                    if not self._cleanup_in_progress:
                        self._cleanup_in_progress = True
                        owns_cleanup = True
                        break
                    waiter = self._new_async_lifecycle_waiter_locked()
                await self._await_async_lifecycle_change(waiter)

            while True:
                with self._lifecycle:
                    if self._active_loads == 0:
                        return
                    waiter = self._new_async_lifecycle_waiter_locked()
                await self._await_async_lifecycle_change(waiter)
        except BaseException:
            if owns_cleanup:
                self._end_cleanup()
            raise

    def _check_size(self, value: Any, *, stage: str) -> None:
        if value is None:
            return
        try:
            size = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid S3 ContentLength during {stage}: {value!r}"
            ) from exc
        if size > self._max_object_bytes:
            raise ObjectTooLarge(
                f"S3 object declares {size} bytes during {stage}, "
                f"limit is {self._max_object_bytes}"
            )

    def _new_temp_file(self):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            delete=False, dir=self._download_dir
        )
        with self._temp_files_lock:
            self._temp_files.append(tmp.name)
        return tmp

    def _release_temp(self, path: str) -> None:
        with self._temp_files_lock, suppress(ValueError):
            self._temp_files.remove(path)

    def _discard_temp(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)
        self._release_temp(path)

    def _finalize_temp(self, path: str, key: str) -> str:
        source_ext = PurePosixPath(key).suffix.lstrip(".").lower()
        source_allowed = is_allowed_extension(source_ext)
        try:
            detected = detect_content_type_from_path(path).lower().lstrip(".")
        except Exception as exc:
            if not source_allowed:
                raise ValueError(
                    f"Unable to determine object content type for key {key!r}"
                ) from exc
            logger.warning(f"对象内容探测失败，回退到 key 后缀 {source_ext!r}: {exc!r}")
            label = source_ext
        else:
            detected_allowed = is_allowed_extension(detected)
            if not detected_allowed:
                raise ValueError(f"Unsupported object content type {detected!r}")
            elif source_allowed and source_ext != detected:
                if detected == "txt" and source_ext in _GENERIC_TEXT_EXTENSIONS:
                    label = source_ext
                else:
                    raise ObjectContentTypeMismatch(
                        f"Object key extension {source_ext!r} does not match "
                        f"downloaded content type {detected!r}"
                    )
            else:
                label = detected
        target = str(Path(path).with_suffix(f".{label}"))
        Path(path).replace(target)
        with self._temp_files_lock:
            self._temp_files[self._temp_files.index(path)] = target
        return target

    def _stream_object(self, response: dict[str, Any], key: str) -> str:
        body = response["Body"]
        try:
            self._check_size(response.get("ContentLength"), stage="get_object")
            tmp = self._new_temp_file()
            total = 0
            try:
                with tmp:
                    for chunk in body.iter_chunks(chunk_size=_STREAM_CHUNK_BYTES):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self._max_object_bytes:
                            raise ObjectTooLarge(
                                f"S3 object exceeded {self._max_object_bytes} bytes "
                                "while streaming"
                            )
                        tmp.write(chunk)
                if total == 0:
                    raise ValueError(f"S3 object is empty: {key!r}")
                return self._finalize_temp(tmp.name, key)
            except BaseException:
                self._discard_temp(tmp.name)
                raise
        finally:
            with suppress(Exception):
                body.close()

    async def _astream_object(self, response: dict[str, Any], key: str) -> str:
        body = response["Body"]
        try:
            self._check_size(response.get("ContentLength"), stage="get_object")
            tmp = self._new_temp_file()
            total = 0
            try:
                async for chunk in body.iter_chunks(chunk_size=_STREAM_CHUNK_BYTES):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self._max_object_bytes:
                        raise ObjectTooLarge(
                            f"S3 object exceeded {self._max_object_bytes} bytes "
                            "while streaming"
                        )
                    await asyncio.to_thread(tmp.write, chunk)
                await asyncio.to_thread(tmp.close)
                if total == 0:
                    raise ValueError(f"S3 object is empty: {key!r}")
                return await asyncio.to_thread(self._finalize_temp, tmp.name, key)
            except BaseException:
                await asyncio.to_thread(tmp.close)
                await asyncio.to_thread(self._discard_temp, tmp.name)
                raise
        finally:
            try:
                result = body.close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(f"关闭 S3 响应流时出错（已忽略）：{exc!r}")

    def _build_content(
        self,
        path: str,
        source: SourceContent,
        location: S3ObjectLocation,
        head: dict[str, Any],
    ) -> LoaderContent:
        file_path = Path(path)
        file_type = file_path.suffix.lstrip(".").lower()
        metadata: dict[str, Any] = {
            "source_type": source.source_type,
            "bucket": location.bucket,
            "object_key": location.key,
            "file_name": PurePosixPath(location.key).name,
            "file_type": file_type,
            "file_size": file_path.stat().st_size,
            "etag": str(head.get("ETag", "")).strip('"') or None,
        }
        try:
            metadata["parse_config"] = ParseConfig.from_extension(file_type)
        except ValueError:
            metadata["parse_config"] = None
        return LoaderContent(
            path=file_path,
            source=source,
            is_temp=True,
            metadata=metadata,
            _release=lambda: self._release_temp(path),
        )

    def load(self, source: SourceContent | str) -> LoaderContent:
        normalized = (
            source if isinstance(source, SourceContent) else SourceContent(source)
        )
        location = parse_s3_uri(normalized.source)
        with self._sync_activity():
            client = self._shared_client()
            head = client.head_object(Bucket=location.bucket, Key=location.key)
            self._check_size(head.get("ContentLength"), stage="head_object")
            response = client.get_object(Bucket=location.bucket, Key=location.key)
            path = self._stream_object(response, location.key)
            try:
                return self._build_content(path, normalized, location, head)
            except BaseException:
                self._discard_temp(path)
                raise

    async def aload(self, source: SourceContent | str) -> LoaderContent:
        normalized = (
            source if isinstance(source, SourceContent) else SourceContent(source)
        )
        location = parse_s3_uri(normalized.source)
        async with self._async_activity():
            client = await self._shared_async_client()
            head = await client.head_object(Bucket=location.bucket, Key=location.key)
            self._check_size(head.get("ContentLength"), stage="head_object")
            response = await client.get_object(Bucket=location.bucket, Key=location.key)
            path = await self._astream_object(response, location.key)
            try:
                return self._build_content(path, normalized, location, head)
            except BaseException:
                self._discard_temp(path)
                raise

    def _cleanup_sync_resources(self) -> None:
        with self._temp_files_lock:
            paths = self._temp_files.copy()
            self._temp_files.clear()
        for path in paths:
            Path(path).unlink(missing_ok=True)
        with self._sync_client_lock:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None

    def cleanup(self) -> None:
        """Wait for active work, delete temp files, and close the owned sync client."""

        self._begin_cleanup()
        try:
            self._cleanup_sync_resources()
        finally:
            self._end_cleanup()

    async def acleanup(self) -> None:
        """Release temporary files and both owned clients without lifecycle races."""

        cleanup_task = asyncio.create_task(self._acleanup_impl())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise

    async def _acleanup_impl(self) -> None:
        await self._abegin_cleanup()
        try:
            await asyncio.to_thread(self._cleanup_sync_resources)
            async with self._async_client_lock:
                if self._owns_async_client and self._async_client is not None:
                    context = self._async_client_context
                    if context is not None:
                        await context.__aexit__(None, None, None)
                    else:
                        result = self._async_client.close()
                        if inspect.isawaitable(result):
                            await result
                    self._async_client = None
                    self._async_client_context = None
        finally:
            self._end_cleanup()

    async def aclose(self) -> None:
        """Backward-compatible alias for ``acleanup``."""

        await self.acleanup()


# Product-facing name retained for MinIO-specific deployments. The implementation
# remains protocol-based and works with AWS S3 and other compatible endpoints.
MinioLoader = S3Loader


__all__ = [
    "DEFAULT_MAX_OBJECT_BYTES",
    "MinioLoader",
    "ObjectContentTypeMismatch",
    "ObjectTooLarge",
    "S3Loader",
    "S3ObjectLocation",
    "parse_s3_uri",
]
