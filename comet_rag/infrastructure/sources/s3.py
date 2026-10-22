from __future__ import annotations

# S3-compatible object-storage adapter. SDK imports stay lazy so the core install
# does not pull an object-storage client into the library runtime.
import asyncio
import inspect
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from comet_rag.engines.documents.formats import ContentTypeMismatch
from comet_rag.engines.offload import run_sync
from comet_rag.infrastructure.sources._lifecycle import AsyncActivity, SyncActivity
from comet_rag.infrastructure.sources.base import AsyncBaseLoader, BaseLoader, finish
from comet_rag.infrastructure.sources.file_info import (
    TemporaryFileRegistry,
    build_file_metadata,
    resolve_downloaded_extension,
)
from comet_rag.ports.source import LoadedResource, SourceContent

LoaderContent = LoadedResource

DEFAULT_MAX_OBJECT_BYTES = 100 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_OBJECT_SCHEMES = frozenset({"s3", "minio"})


class ObjectTooLarge(ValueError):
    """The object exceeds the configured application-level size limit."""


ObjectContentTypeMismatch = ContentTypeMismatch


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


class _S3Settings:
    """S3 地址、大小预算和临时文件规则；SDK 类型不跨执行模型共享。"""

    def _configure(
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
        self._temporary_files = TemporaryFileRegistry(self._download_dir)

    @property
    def temp_files(self) -> list[str]:
        return self._temporary_files.paths

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
        return self._temporary_files.create()

    def _release_temp(self, path: str) -> None:
        self._temporary_files.release(path)

    def _discard_temp(self, path: str) -> None:
        self._temporary_files.discard(path)

    def _finalize_temp(self, path: str, key: str) -> str:
        label = resolve_downloaded_extension(path, PurePosixPath(key).name)
        return self._temporary_files.replace_suffix(path, label)

    def _build_content(
        self,
        path: str,
        source: SourceContent,
        location: S3ObjectLocation,
        head: dict[str, Any],
    ) -> LoaderContent:
        file_path = Path(path)
        metadata = build_file_metadata(
            file_path,
            source,
            file_name=PurePosixPath(location.key).name,
            extra={
                "bucket": location.bucket,
                "object_key": location.key,
                "etag": str(head.get("ETag", "")).strip('"') or None,
            },
        )
        return LoaderContent(
            path=file_path,
            source=source,
            is_temp=True,
            metadata=metadata,
            _release=lambda: self._release_temp(path),
        )

    def _cleanup_files(self) -> None:
        try:
            self._temporary_files.cleanup()
        except OSError as exc:
            logger.warning(f"临时文件清理失败，已保留登记以便重试: {exc!r}")


class S3Loader(_S3Settings, SyncActivity, BaseLoader):
    """同步 boto3 来源；客户端懒创建并由其创建者关闭。"""

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
        client: Any = None,  # SDK 的服务类型由运行时生成。
    ) -> None:
        self._configure(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            region_name=region_name,
            addressing_style=addressing_style,
            verify_ssl=verify_ssl,
            max_object_bytes=max_object_bytes,
            download_dir=download_dir,
        )
        self._client = client
        self._owns_client = client is None
        self._initialize_lifecycle()
        self._client_lock = threading.Lock()

    def _new_client(self) -> Any:
        try:
            import boto3  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "S3Loader 需要可选依赖，请安装 comet-rag[server] 或 comet-rag[all]"
            ) from exc

        return boto3.client("s3", **self._client_kwargs())

    def _shared_client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                self._client = self._new_client()
            return self._client

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

    def _load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        self._reject_unsupported(kwargs)
        normalized = (
            source if isinstance(source, SourceContent) else SourceContent(source)
        )
        location = parse_s3_uri(normalized.source)
        with self._activity():
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


class AsyncS3Loader(_S3Settings, AsyncActivity, AsyncBaseLoader):
    """原生 aioboto3 来源；异步等待不占用默认线程池。"""

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
        client: Any = None,  # SDK 的服务类型由运行时生成。
    ) -> None:
        self._configure(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            region_name=region_name,
            addressing_style=addressing_style,
            verify_ssl=verify_ssl,
            max_object_bytes=max_object_bytes,
            download_dir=download_dir,
        )
        self._client = client
        self._owns_client = client is None
        self._initialize_lifecycle()
        self._client_lock = asyncio.Lock()
        self._client_context: Any = None  # aioboto3 动态生成的客户端上下文。

    def _new_client_context(self) -> Any:
        try:
            import aioboto3  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "S3Loader 异步入口需要可选依赖，请安装 "
                "comet-rag[server] 或 comet-rag[all]"
            ) from exc

        return aioboto3.Session().client("s3", **self._client_kwargs())

    async def _shared_client(self) -> Any:
        async with self._client_lock:
            if self._client is None:
                context = self._new_client_context()
                try:
                    client = await context.__aenter__()
                except BaseException:
                    with suppress(Exception):
                        await context.__aexit__(None, None, None)
                    raise
                self._client_context = context
                self._client = client
            return self._client

    async def _stream_object(self, response: dict[str, Any], key: str) -> str:
        body = response["Body"]
        try:
            self._check_size(response.get("ContentLength"), stage="get_object")
            tmp = self._new_temp_file()
            path = tmp.name
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
                    await run_sync(tmp.write, chunk)
                await run_sync(tmp.close)
                if total == 0:
                    raise ValueError(f"S3 object is empty: {key!r}")

                def finalize() -> str:
                    nonlocal path
                    path = self._finalize_temp(path, key)
                    return path

                return await run_sync(finalize)
            except BaseException:

                def discard() -> None:
                    tmp.close()
                    self._discard_temp(path)

                await run_sync(discard)
                raise
        finally:
            try:
                result = body.close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(f"关闭 S3 响应流时出错（已忽略）：{exc!r}")

    async def _load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        self._reject_unsupported(kwargs)
        normalized = (
            source if isinstance(source, SourceContent) else SourceContent(source)
        )
        location = parse_s3_uri(normalized.source)
        async with self._activity():
            client = await self._shared_client()
            head = await client.head_object(Bucket=location.bucket, Key=location.key)
            self._check_size(head.get("ContentLength"), stage="head_object")
            response = await client.get_object(Bucket=location.bucket, Key=location.key)
            path = await self._stream_object(response, location.key)
            try:
                return await run_sync(
                    self._build_content, path, normalized, location, head
                )
            except BaseException:
                await run_sync(self._discard_temp, path)
                raise

    async def cleanup(self) -> None:
        await finish(asyncio.create_task(self._cleanup()))

    async def _cleanup(self) -> None:
        await self._begin_cleanup()
        try:
            await run_sync(self._cleanup_files)
            async with self._client_lock:
                if self._owns_client and self._client is not None:
                    if self._client_context is not None:
                        await self._client_context.__aexit__(None, None, None)
                    else:
                        result = self._client.close()
                        if inspect.isawaitable(result):
                            await result
                    self._client = None
                    self._client_context = None
        finally:
            self._end_cleanup()


MinioLoader = S3Loader
AsyncMinioLoader = AsyncS3Loader

__all__ = [
    "DEFAULT_MAX_OBJECT_BYTES",
    "MinioLoader",
    "AsyncMinioLoader",
    "ObjectContentTypeMismatch",
    "ObjectTooLarge",
    "S3Loader",
    "AsyncS3Loader",
    "S3ObjectLocation",
    "parse_s3_uri",
]
