from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote

from comet_rag._async import run_sync
from comet_rag.documents.schemas import SourceInfo
from comet_rag.errors import SourceError, SourceResourceLimitError
from comet_rag.identity import stable_id

from .base import (
    _encode_source_version,
    _parse_content_length,
    _temporary_directory,
    _validate_positive_limit,
)
from .types import LocalResource, S3Source, Source


class _S3Client(Protocol):
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


class AsyncS3SourceResolver:
    """阻塞 SDK 排干后才释放下载目录；注入客户端由创建者关闭。"""

    def __init__(
        self,
        client: _S3Client,
        *,
        namespace: str,
        max_bytes: int = 256 * 1024 * 1024,
        temp_dir: Path | None = None,
    ) -> None:
        if not isinstance(namespace, str):
            raise ValueError("S3 namespace must be a string")
        if not namespace.strip():
            raise ValueError("S3 namespace must be nonblank")
        if not callable(getattr(client, "get_object", None)):
            raise TypeError("S3 source requires an injected synchronous S3 client")
        _validate_positive_limit("max_bytes", max_bytes)
        self._client = client
        self._namespace = namespace
        self._max_bytes = max_bytes
        self._temp_dir = temp_dir

    @asynccontextmanager
    async def resolve(self, source: Source) -> AsyncIterator[LocalResource]:
        if not isinstance(source, S3Source):
            raise TypeError("S3 resolver requires S3Source")
        with _temporary_directory(self._temp_dir) as directory:
            yield await run_sync(self._download, source, directory / "resource")

    def _download(self, source: S3Source, destination: Path) -> LocalResource:
        arguments = {"Bucket": source.bucket, "Key": source.key}
        if source.version_id is not None:
            arguments["VersionId"] = source.version_id
        try:
            response = self._client.get_object(**arguments)
        except Exception as exc:
            raise SourceError(f"S3 get_object failed: {type(exc).__name__}") from exc
        body = response.get("Body")
        if body is None:
            raise SourceError("S3 response has no closable body")
        if not callable(getattr(body, "close", None)):
            raise SourceError("S3 response has no closable body")
        try:
            if not callable(getattr(body, "read", None)):
                raise SourceError("S3 response has no readable body")
            length = _parse_content_length(response.get("ContentLength"))
            if length is not None and length > self._max_bytes:
                raise SourceResourceLimitError("S3 ContentLength exceeds byte budget")
            digest = hashlib.sha256()
            size = 0
            with destination.open("xb") as stream:
                while True:
                    chunk = body.read(min(1024 * 1024, self._max_bytes - size + 1))
                    if not isinstance(chunk, bytes):
                        raise SourceError("S3 body must yield bytes")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise SourceResourceLimitError("S3 body exceeds byte budget")
                    if length is not None and size > length:
                        raise SourceError("S3 body exceeds ContentLength")
                    stream.write(chunk)
                    digest.update(chunk)
            if length is not None and size != length:
                raise SourceError("S3 body length does not match ContentLength")
            metadata: dict[str, object] = {}
            for field, key in (("VersionId", "version_id"), ("ETag", "etag")):
                value = response.get(field)
                if value is not None:
                    if not isinstance(value, str):
                        raise SourceError(f"invalid S3 {field}")
                    if not value:
                        raise SourceError(f"invalid S3 {field}")
                    metadata[key] = value
            if source.version_id is not None:
                if metadata.get("version_id") != source.version_id:
                    raise SourceError(
                        "S3 response does not confirm requested VersionId"
                    )
            modified = response.get("LastModified")
            if modified is not None:
                if not isinstance(modified, datetime):
                    raise SourceError("invalid S3 LastModified")
                metadata["last_modified"] = modified.isoformat()
            media_type = response.get("ContentType")
            if media_type is not None:
                if not isinstance(media_type, str):
                    raise SourceError("invalid S3 ContentType")
                if not media_type.strip():
                    raise SourceError("invalid S3 ContentType")
            resource = LocalResource(
                path=destination,
                source=SourceInfo(
                    source.source_id
                    or stable_id(
                        "source",
                        {
                            "s3": {
                                "namespace": self._namespace,
                                "bucket": source.bucket,
                                "key": source.key,
                            }
                        },
                    ),
                    digest.hexdigest(),
                    locator=f"s3://{quote(source.bucket, safe='')}/{quote(source.key, safe='/')}",
                    source_version=_encode_source_version(metadata),
                ),
                original_name=PurePosixPath(source.key).name or None,
                media_type=media_type,
                size_bytes=size,
            )
        except BaseException as exc:
            try:
                body.close()
            except Exception as cleanup:
                exc.add_note(f"S3 response body close failed: {type(cleanup).__name__}")
            raise
        else:
            body.close()
            return resource
