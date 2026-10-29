from __future__ import annotations

import hashlib
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from comet_rag._urls import has_url_credentials
from comet_rag.documents.schemas import SourceInfo
from comet_rag.identity import stable_id

from .base import (
    SourceError,
    SourceResourceLimitExceeded,
    _content_length,
    _positive_limit,
    _temporary_directory,
    _version,
)
from .types import HttpSource, LocalResource, Source


class AsyncHttpSourceResolver:
    """逐跳调用准入策略；客户端与实际网络连接约束由创建者管理。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        policy: Callable[[HttpSource], bool | None],
        max_bytes: int = 256 * 1024 * 1024,
        max_redirects: int = 5,
        temp_dir: Path | None = None,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("HTTP source requires an injected httpx.AsyncClient")
        if not callable(policy):
            raise TypeError("HTTP source requires an admission policy")
        _positive_limit("max_bytes", max_bytes)
        if type(max_redirects) is not int or max_redirects < 0:
            raise ValueError("max_redirects must be a nonnegative integer")
        self._client = client
        self._policy = policy
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._temp_dir = temp_dir

    @asynccontextmanager
    async def resolve(self, source: Source) -> AsyncIterator[LocalResource]:
        if not isinstance(source, HttpSource):
            raise TypeError("HTTP resolver requires HttpSource")
        with _temporary_directory(self._temp_dir) as directory:
            yield await self._download(source, directory / "resource")

    async def _download(self, source: HttpSource, destination: Path) -> LocalResource:
        request = self._client.build_request(
            "GET", source.url, headers={"Accept-Encoding": "identity"}
        )
        sensitive = has_url_credentials(source.url, include_key=True)
        for redirects in range(self._max_redirects + 1):
            hop = HttpSource(str(request.url), source.source_id)
            sensitive |= has_url_credentials(hop.url, include_key=True)
            if self._policy(hop) is False:
                raise SourceError("HTTP source rejected by admission policy")
            try:
                response = await self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                    auth=self._client.auth if redirects == 0 else None,
                )
            except httpx.HTTPError as exc:
                raise SourceError(
                    f"HTTP source request failed: {type(exc).__name__}"
                ) from exc
            try:
                if response.is_redirect:
                    if redirects >= self._max_redirects:
                        raise SourceError("HTTP source exceeded redirect limit")
                    next_request = response.next_request
                    if next_request is None:
                        raise SourceError("HTTP redirect has no valid Location")
                    request = next_request
                    continue
                if not 200 <= response.status_code < 300 or response.status_code == 206:
                    raise SourceError(
                        f"HTTP source returned status {response.status_code}"
                    )
                encoding = (
                    response.headers.get("content-encoding", "identity").strip().lower()
                )
                if encoding not in {"", "identity"}:
                    raise SourceError("HTTP source must deliver identity-encoded bytes")
                length = _content_length(response.headers.get("content-length"))
                if length is not None and length > self._max_bytes:
                    raise SourceResourceLimitExceeded(
                        "HTTP Content-Length exceeds byte budget"
                    )
                digest = hashlib.sha256()
                size = 0
                try:
                    with destination.open("xb") as stream:
                        async for chunk in response.aiter_raw():
                            size += len(chunk)
                            if size > self._max_bytes:
                                raise SourceResourceLimitExceeded(
                                    "HTTP body exceeds byte budget"
                                )
                            if length is not None and size > length:
                                raise SourceError("HTTP body exceeds Content-Length")
                            stream.write(chunk)
                            digest.update(chunk)
                except httpx.HTTPError as exc:
                    raise SourceError(
                        f"HTTP source stream failed: {type(exc).__name__}"
                    ) from exc
                if length is not None and size != length:
                    raise SourceError("HTTP body length does not match Content-Length")
                media_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip()
                    or None
                )
                name = (
                    PurePosixPath(unquote(urlsplit(str(response.url)).path)).name
                    or None
                )
                return LocalResource(
                    path=destination,
                    source=SourceInfo(
                        source.source_id
                        or stable_id("source", {"http": str(httpx.URL(source.url))}),
                        digest.hexdigest(),
                        locator=None if sensitive else str(httpx.URL(source.url)),
                        source_version=_version(
                            {
                                "etag": response.headers.get("etag"),
                                "last_modified": response.headers.get("last-modified"),
                            }
                        ),
                    ),
                    original_name=name,
                    media_type=media_type,
                    size_bytes=size,
                )
            finally:
                active = sys.exception()
                try:
                    await response.aclose()
                except Exception as cleanup:
                    if active is None:
                        raise
                    active.add_note(
                        f"HTTP response close failed: {type(cleanup).__name__}"
                    )
        raise SourceError("HTTP source exceeded redirect limit")
