from __future__ import annotations

import hashlib
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

from comet_rag._async import close_async
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


class _BudgetStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in self._stream:
            size += len(chunk)
            if size > self._max_bytes:
                raise SourceResourceLimitExceeded("HTTP body exceeds byte budget")
            yield chunk

    async def aclose(self) -> None:
        # httpx 在关闭底层流前标记 response 已关闭，取消后不能靠重试补关。
        await close_async(self._stream.aclose(), sys.exception(), "HTTP response")


class _BudgetAuth(httpx.Auth):
    def __init__(self, auth: httpx.Auth | None, max_bytes: int) -> None:
        self._auth = auth if auth is not None else httpx.Auth()
        self._max_bytes = max_bytes
        self._flow: AsyncGenerator[httpx.Request, httpx.Response] | None = None

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        flow = self._auth.async_auth_flow(request)
        self._flow = flow
        request = await anext(flow)
        while True:
            response = yield request
            # 认证握手会在 client.send 返回前读取响应，也必须应用流预算。
            if not isinstance(response.stream, httpx.AsyncByteStream):
                raise TypeError("HTTP source requires an asynchronous response stream")
            response.stream = _BudgetStream(response.stream, self._max_bytes)
            encoding = (
                response.headers.get("content-encoding", "identity").strip().lower()
            )
            if encoding not in {"", "identity"}:
                raise SourceError("HTTP source must deliver identity-encoded bytes")
            try:
                request = await flow.asend(response)
            except StopAsyncIteration:
                return

    async def aclose(self, active: BaseException | None) -> None:
        # 在 send 的异常上下文关闭；生成器内只会看到 HTTPX 注入的 GeneratorExit。
        if self._flow is not None:
            await close_async(self._flow.aclose(), active, "HTTP auth")


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
        initial_url = str(request.url)
        origin = (request.url.scheme, request.url.host, request.url.port)
        auth_allowed = True
        sensitive = has_url_credentials(initial_url, include_key=True)
        for redirects in range(self._max_redirects + 1):
            hop = HttpSource(str(request.url), source.source_id)
            sensitive |= has_url_credentials(hop.url, include_key=True)
            if self._policy(hop) is False:
                raise SourceError("HTTP source rejected by admission policy")
            auth = _BudgetAuth(
                self._client.auth if auth_allowed else None, self._max_bytes
            )
            try:
                try:
                    response = await self._client.send(
                        request, stream=True, follow_redirects=False, auth=auth
                    )
                except BaseException:
                    await auth.aclose(sys.exception())
                    raise
                else:
                    try:
                        await auth.aclose(None)
                    except BaseException:
                        await close_async(
                            response.aclose(), sys.exception(), "HTTP response"
                        )
                        raise
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
                    auth_allowed &= (
                        request.url.scheme,
                        request.url.host,
                        request.url.port,
                    ) == origin
                    if auth_allowed and self._client.auth is not None:
                        request.headers.pop("Authorization", None)
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
                        chunks = (
                            httpx.ByteStream(response.content)
                            if response.is_stream_consumed
                            else response.aiter_raw()
                        )
                        async for chunk in chunks:
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
                        source.source_id or stable_id("source", {"http": initial_url}),
                        digest.hexdigest(),
                        locator=None if sensitive else initial_url,
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
