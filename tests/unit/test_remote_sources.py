from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.sources import (
    AsyncLocalSourceResolver,
    AsyncS3SourceResolver,
    AsyncSourceResolver,
    AsyncSourceRouter,
    HttpSource,
    LocalSource,
    S3Source,
    SourceError,
)
from comet_rag.sources.http import AsyncHttpSourceResolver


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class S3Client:
    def __init__(self, body: io.BytesIO, **metadata: object) -> None:
        self.body = body
        self.metadata = metadata
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Body": self.body, **self.metadata}

    def close(self) -> None:
        self.closed = True


def allow(source: HttpSource) -> bool:
    return True


def test_http_streams_exact_bytes_metadata_cleanup_and_client_ownership(
    tmp_path: Path,
) -> None:
    data = b"  text\r\n\x00bytes\n"
    body = Stream((data[:4], data[4:]))
    checked: list[str] = []

    def policy(source: HttpSource) -> None:
        checked.append(source.url)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(data)),
                "Content-Type": "text/plain; charset=utf-8",
                "ETag": '"upstream"',
                "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            },
            stream=body,
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            resolver = AsyncHttpSourceResolver(client, policy=policy, temp_dir=tmp_path)
            source = HttpSource("https://example.org/a/report.txt?edition=2")
            async with resolver.resolve(source) as resource:
                assert resource.path.read_bytes() == data
                assert resource.source.content_hash == hashlib.sha256(data).hexdigest()
                assert resource.source.locator == source.url
                assert resource.size_bytes == len(data)
                assert resource.media_type == "text/plain"
                assert resource.original_name == "report.txt"
                assert json.loads(resource.source.source_version or "null") == {
                    "etag": '"upstream"',
                    "last_modified": "Wed, 21 Oct 2015 07:28:00 GMT",
                }
                path = resource.path
                assert not client.is_closed
            assert body.closed and not path.exists()
            assert list(tmp_path.iterdir()) == []
            assert not client.is_closed
        assert checked == ["https://example.org/a/report.txt?edition=2"]

    asyncio.run(run())


def test_http_policy_validates_each_redirect_before_request_and_strips_cross_origin_auth(
    tmp_path: Path,
) -> None:
    sent: list[str] = []
    checked: list[str] = []

    def policy(source: HttpSource) -> bool:
        checked.append(source.url)
        return source.url != "https://blocked.example/final"

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        if len(sent) == 1:
            return httpx.Response(
                302, headers={"Location": "https://other.example/next"}
            )
        assert "authorization" not in request.headers
        return httpx.Response(
            307, headers={"Location": "https://blocked.example/final"}
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            headers={"Authorization": "Bearer injected"},
            follow_redirects=True,
        ) as client:
            resolver = AsyncHttpSourceResolver(client, policy=policy, temp_dir=tmp_path)
            with pytest.raises(SourceError, match="policy"):
                async with resolver.resolve(
                    HttpSource("https://initial.example/start")
                ):
                    pytest.fail("blocked redirect was delivered")
            assert not client.is_closed

    asyncio.run(run())
    assert checked == [
        "https://initial.example/start",
        "https://other.example/next",
        "https://blocked.example/final",
    ]
    assert sent == checked[:2]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("status", "headers", "chunks", "match"),
    [
        (500, {}, (b"error",), "status 500"),
        (206, {}, (b"partial",), "status 206"),
        (200, {"Content-Length": "100"}, (b"a",), "budget"),
        (200, {"Content-Length": "4"}, (b"ab",), "length"),
        (200, {"Content-Length": "1"}, (b"ab",), "Content-Length"),
        (200, {"Content-Length": "-1"}, (b"a",), "Content-Length"),
        (200, {"Content-Length": "two"}, (b"a",), "Content-Length"),
        (200, {}, (b"ab", b"cd", b"e"), "budget"),
        (200, {"Content-Encoding": "gzip"}, (b"a",), "identity"),
    ],
)
def test_http_errors_never_deliver_or_leave_partial_files(
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    chunks: tuple[bytes, ...],
    match: str,
) -> None:
    body = Stream(chunks)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, headers=headers, stream=body)
            )
        ) as client:
            resolver = AsyncHttpSourceResolver(
                client, policy=allow, max_bytes=4, temp_dir=tmp_path
            )
            with pytest.raises(SourceError, match=match):
                async with resolver.resolve(HttpSource("https://example.org/file")):
                    pytest.fail("invalid response delivered")
            assert not client.is_closed

    asyncio.run(run())
    assert body.closed
    assert list(tmp_path.iterdir()) == []


def test_http_sensitive_queries_require_explicit_identity_and_never_persist_url(
    tmp_path: Path,
) -> None:
    for query in (
        "token=private",
        "X-Amz-Signature=private",
        "api_key=private",
        "AWSAccessKeyId=private",
    ):
        with pytest.raises(ValueError, match="source_id"):
            HttpSource(f"https://example.org/file?{query}")
    source = HttpSource(
        "https://example.org/file?X-Amz-Signature=private", source_id="logical"
    )
    assert "private" not in repr(source)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Stream((b"ok",)))
            )
        ) as client:
            resolver = AsyncHttpSourceResolver(client, policy=allow, temp_dir=tmp_path)
            async with resolver.resolve(source) as resource:
                assert resource.source.locator is None
                assert resource.source.source_id == "logical"
                assert "X-Amz-Signature" not in repr(resource)

    asyncio.run(run())


def test_http_query_identity_and_redirect_limit(tmp_path: Path) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Stream((b"same",)))
            )
        ) as client:
            resolver = AsyncHttpSourceResolver(client, policy=allow, temp_dir=tmp_path)
            async with (
                resolver.resolve(
                    HttpSource("https://example.org/file?edition=1")
                ) as first,
                resolver.resolve(
                    HttpSource("https://example.org/file?edition=2")
                ) as second,
            ):
                assert first.source.content_hash == second.source.content_hash
                assert first.source.source_id != second.source.source_id
                assert first.path != second.path
        calls: list[str] = []

        def redirect(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(302, headers={"Location": "/again"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
            resolver = AsyncHttpSourceResolver(
                client, policy=allow, max_redirects=1, temp_dir=tmp_path
            )
            with pytest.raises(SourceError, match="redirect limit"):
                async with resolver.resolve(HttpSource("https://example.org/start")):
                    pytest.fail("redirect loop delivered")
        assert calls == ["https://example.org/start", "https://example.org/again"]

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []


def test_http_cancellation_closes_stream_and_cleans_temp(tmp_path: Path) -> None:
    class Blocking(Stream):
        def __init__(self) -> None:
            super().__init__((b"partial",))
            self.started = asyncio.Event()

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial"
            self.started.set()
            await asyncio.Event().wait()

    async def run() -> None:
        body = Blocking()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=body)
            )
        ) as client:
            resolver = AsyncHttpSourceResolver(client, policy=allow, temp_dir=tmp_path)

            async def enter() -> None:
                async with resolver.resolve(HttpSource("https://example.org/file")):
                    pytest.fail("cancelled resource delivered")

            task = asyncio.create_task(enter())
            await asyncio.wait_for(body.started.wait(), 3)
            assert list(tmp_path.iterdir())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert body.closed
            assert not client.is_closed
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_s3_hashes_actual_bytes_and_preserves_versions_and_namespace(
    tmp_path: Path,
) -> None:
    data = b"S3\r\nactual bytes"
    modified = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    client = S3Client(
        io.BytesIO(data),
        ContentLength=len(data),
        ContentType="text/plain",
        VersionId="version-1",
        ETag='"etag-not-hash"',
        LastModified=modified,
    )

    async def run() -> None:
        resolver = AsyncS3SourceResolver(
            client, namespace="account-a", temp_dir=tmp_path
        )
        async with resolver.resolve(
            S3Source("bucket", "reports/report.txt", version_id="version-1")
        ) as resource:
            assert resource.path.read_bytes() == data
            assert resource.source.content_hash == hashlib.sha256(data).hexdigest()
            assert resource.source.locator == "s3://bucket/reports/report.txt"
            assert json.loads(resource.source.source_version or "null") == {
                "version_id": "version-1",
                "etag": '"etag-not-hash"',
                "last_modified": modified.isoformat(),
            }
            assert resource.original_name == "report.txt"
            assert resource.media_type == "text/plain"
            assert resource.size_bytes == len(data)
            path = resource.path
            identity = resource.source.source_id
        assert client.body.closed and not client.closed and not path.exists()
        assert client.calls == [
            {"Bucket": "bucket", "Key": "reports/report.txt", "VersionId": "version-1"}
        ]
        other = S3Client(io.BytesIO(data), ContentLength=len(data))
        async with AsyncS3SourceResolver(
            other, namespace="account-b", temp_dir=tmp_path
        ).resolve(S3Source("bucket", "reports/report.txt")) as resource:
            assert resource.source.source_id != identity
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("data", "metadata", "match"),
    [
        (b"abc", {"ContentLength": 100}, "budget"),
        (b"abcde", {}, "budget"),
        (b"abc", {"ContentLength": 2}, "ContentLength"),
        (b"abc", {"ContentLength": 4}, "ContentLength"),
        (b"abc", {"ContentLength": -1}, "Content-Length"),
        (b"abc", {"VersionId": 3}, "VersionId"),
        (b"abc", {"ContentType": ""}, "ContentType"),
    ],
)
def test_s3_failures_close_body_and_clear_partial_files(
    tmp_path: Path, data: bytes, metadata: dict[str, object], match: str
) -> None:
    client = S3Client(io.BytesIO(data), **metadata)

    async def run() -> None:
        with pytest.raises(SourceError, match=match):
            async with AsyncS3SourceResolver(
                client, namespace="test", max_bytes=4, temp_dir=tmp_path
            ).resolve(S3Source("bucket", "key")):
                pytest.fail("invalid S3 response delivered")

    asyncio.run(run())
    assert client.body.closed and not client.closed
    assert list(tmp_path.iterdir()) == []


def test_s3_cancellation_drains_thread_before_temp_cleanup(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    observed: list[bool] = []

    class BlockingBody(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            if not entered.is_set():
                entered.set()
                assert release.wait(3)
                observed.append(bool(list(tmp_path.iterdir())))
            return super().read(size)

    client = S3Client(BlockingBody(b"data"), ContentLength=4)

    async def run() -> None:
        resolver = AsyncS3SourceResolver(client, namespace="test", temp_dir=tmp_path)

        async def enter() -> None:
            async with resolver.resolve(S3Source("bucket", "key")):
                pytest.fail("cancelled S3 resource delivered")

        task = asyncio.create_task(enter())
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert observed == [True]
    assert client.body.closed and not client.closed
    assert list(tmp_path.iterdir()) == []


def test_router_uses_explicit_source_types_and_configured_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local.txt"
    path.write_bytes(b"local")
    local: AsyncSourceResolver = AsyncLocalSourceResolver()
    router = AsyncSourceRouter(local=local)

    async def run() -> None:
        async with router.resolve(LocalSource(path)) as resource:
            assert resource.path == path
        with pytest.raises(ValueError, match="no resolver"):
            router.resolve(HttpSource("https://example.org"))
        with pytest.raises(ValueError, match="no resolver"):
            router.resolve(S3Source("bucket", "key"))

    asyncio.run(run())
    assert path.read_bytes() == b"local"


def test_http_client_auth_is_not_reapplied_to_cross_origin_redirect(
    tmp_path: Path,
) -> None:
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return (
            httpx.Response(302, headers={"Location": "https://other.example/final"})
            if len(seen) == 1
            else httpx.Response(200, stream=Stream((b"ok",)))
        )

    async def run() -> None:
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(handle), auth=("user", "pass")
            ) as client,
            AsyncHttpSourceResolver(client, policy=allow, temp_dir=tmp_path).resolve(
                HttpSource("https://example.org/start")
            ) as resource,
        ):
            assert resource.path.read_bytes() == b"ok"

    asyncio.run(run())
    assert seen[0] is not None and seen[0].startswith("Basic ")
    assert seen[1] is None


def test_redirect_to_sensitive_query_requires_original_explicit_source_id(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return (
            httpx.Response(302, headers={"Location": "/file?token=hidden"})
            if len(calls) == 1
            else httpx.Response(200, stream=Stream((b"ok",)))
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            resolver = AsyncHttpSourceResolver(client, policy=allow, temp_dir=tmp_path)
            with pytest.raises(ValueError, match="source_id"):
                async with resolver.resolve(HttpSource("https://example.org/start")):
                    pytest.fail("implicit signed redirect delivered")
            assert len(calls) == 1
            calls.clear()
            async with resolver.resolve(
                HttpSource("https://example.org/start", "logical")
            ) as resource:
                assert resource.source.locator is None
                assert resource.source.source_id == "logical"
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_http_preserves_primary_error_when_response_cleanup_fails(
    tmp_path: Path,
) -> None:
    class BrokenClose(Stream):
        async def aclose(self) -> None:
            self.closed = True
            raise OSError("close failed")

    async def run() -> None:
        body = BrokenClose((b"error",))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, stream=body)
            )
        ) as client:
            with pytest.raises(SourceError, match="status 500") as captured:
                async with AsyncHttpSourceResolver(
                    client, policy=allow, temp_dir=tmp_path
                ).resolve(HttpSource("https://example.org/file")):
                    pytest.fail("HTTP failure delivered")
            assert any("close failed" in note for note in captured.value.__notes__)
        assert body.closed
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_s3_invalid_stream_and_requested_version_never_deliver(tmp_path: Path) -> None:
    class InvalidBody(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return None  # type: ignore[return-value]

    invalid = S3Client(InvalidBody())
    mismatch = S3Client(io.BytesIO(b"ok"), VersionId="different")

    async def run() -> None:
        for client, match in ((invalid, "yield bytes"), (mismatch, "VersionId")):
            with pytest.raises(SourceError, match=match):
                async with AsyncS3SourceResolver(
                    client, namespace="test", temp_dir=tmp_path
                ).resolve(S3Source("bucket", "key", "wanted")):
                    pytest.fail("invalid S3 response delivered")
            assert client.body.closed
            assert not client.closed
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "http://user:private@example.org",  # pragma: allowlist secret
        "http://example.org/#fragment",
        "http://example.org:99999",
        "http://example.org/space here",
    ],
)
def test_http_source_rejects_invalid_persistent_input(url: str) -> None:
    with pytest.raises(ValueError):
        HttpSource(url)
