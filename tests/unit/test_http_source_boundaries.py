from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import httpx
import pytest

from comet_rag.sources import HttpSource, SourceError, SourceResourceLimitExceeded
from comet_rag.sources.http import AsyncHttpSourceResolver


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


CHALLENGE = 'Digest realm="example", nonce="nonce", algorithm=MD5, qop="auth"'


def test_compressed_auth_challenge_is_rejected_before_reading(tmp_path: Path) -> None:
    body = Body((gzip.compress(b"x" * 100_000),))

    def handle(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": CHALLENGE, "Content-Encoding": "gzip"},
                stream=body,
            )
        return httpx.Response(200, stream=Body((b"ok",)))

    async def run() -> None:
        async with httpx.AsyncClient(
            auth=httpx.DigestAuth("user", "pass"), transport=httpx.MockTransport(handle)
        ) as client:
            with pytest.raises(SourceError, match="identity"):
                async with AsyncHttpSourceResolver(
                    client,
                    policy=lambda source: True,
                    max_bytes=1024,
                    temp_dir=tmp_path,
                ).resolve(HttpSource("https://example.org/file")):
                    pytest.fail("compressed challenge accepted")
        assert body.read == 0 and body.closed
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize("close_failure", [False, True])
def test_auth_flow_cleanup_preserves_download_failure(
    tmp_path: Path, close_failure: bool
) -> None:
    async def run() -> None:
        closing = asyncio.Event()
        release = asyncio.Event()
        closed: list[bool] = []

        class Auth(httpx.Auth):
            async def async_auth_flow(
                self, request: httpx.Request
            ) -> AsyncGenerator[httpx.Request, httpx.Response]:
                try:
                    yield request
                    yield request
                finally:
                    closing.set()
                    if not close_failure:
                        await release.wait()
                    closed.append(True)
                    if close_failure:
                        raise OSError("auth cleanup failed")

        async with httpx.AsyncClient(
            auth=Auth(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Body((b"oversize",)))
            ),
        ) as client:

            async def enter() -> None:
                async with AsyncHttpSourceResolver(
                    client, policy=lambda source: True, max_bytes=4, temp_dir=tmp_path
                ).resolve(HttpSource("https://example.org/file")):
                    pytest.fail("oversize auth response accepted")

            task = asyncio.create_task(enter())
            try:
                await asyncio.wait_for(closing.wait(), 3)
                if not close_failure:
                    for _ in range(3):
                        task.cancel()
                        await asyncio.sleep(0)
                        assert not task.done()
                release.set()
                with pytest.raises(SourceResourceLimitExceeded) as captured:
                    await task
                assert closed == [True]
                if close_failure:
                    assert any(
                        "auth close failed" in note for note in captured.value.__notes__
                    )
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_cancellation_survives_stream_close_failure(tmp_path: Path) -> None:
    async def run() -> None:
        closing = asyncio.Event()
        release = asyncio.Event()

        class BrokenClose(Body):
            async def aclose(self) -> None:
                closing.set()
                await release.wait()
                self.closed = True
                raise OSError("close failed")

        body = BrokenClose((b"ok",))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=body)
            )
        ) as client:

            async def enter() -> None:
                async with AsyncHttpSourceResolver(
                    client, policy=lambda source: True, temp_dir=tmp_path
                ).resolve(HttpSource("https://example.org/file")):
                    pytest.fail("cancelled resource delivered")

            task = asyncio.create_task(enter())
            try:
                await asyncio.wait_for(closing.wait(), 3)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError) as captured:
                    await task
                assert body.closed
                assert any("close failed" in note for note in captured.value.__notes__)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_http_identity_includes_effective_initial_query(tmp_path: Path) -> None:
    async def run() -> None:
        ids: list[str] = []
        for edition in ("1", "2", "1"):
            async with httpx.AsyncClient(
                params={"edition": edition},
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, stream=Body((request.url.params["edition"].encode(),))
                    )
                ),
            ) as client:
                resolver = AsyncHttpSourceResolver(
                    client, policy=lambda source: True, temp_dir=tmp_path
                )
                async with resolver.resolve(
                    HttpSource("https://example.org/file")
                ) as resource:
                    assert (
                        resource.source.locator
                        == f"https://example.org/file?edition={edition}"
                    )
                    assert resource.path.read_bytes() == edition.encode()
                    ids.append(resource.source.source_id)
        assert ids[0] == ids[2] and ids[0] != ids[1]
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize("challenge_body", [b"ok", b"123456789"])
def test_digest_challenge_respects_download_budget(
    tmp_path: Path, challenge_body: bytes
) -> None:
    body = Body((challenge_body, b"more"))
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "authorization" not in request.headers:
            return httpx.Response(
                401, headers={"WWW-Authenticate": CHALLENGE}, stream=body
            )
        return httpx.Response(200, stream=Body((b"ok",)))

    async def run() -> None:
        async with httpx.AsyncClient(
            auth=httpx.DigestAuth("user", "pass"), transport=httpx.MockTransport(handle)
        ) as client:
            resolver = AsyncHttpSourceResolver(
                client, policy=lambda source: True, max_bytes=8, temp_dir=tmp_path
            )
            if len(challenge_body) + 4 > 8:
                with pytest.raises(SourceResourceLimitExceeded):
                    async with resolver.resolve(
                        HttpSource("https://example.org/protected")
                    ):
                        pytest.fail("oversize authentication response accepted")
                assert len(calls) == 1
            else:
                async with resolver.resolve(
                    HttpSource("https://example.org/protected")
                ) as resource:
                    assert resource.path.read_bytes() == b"ok"
            assert body.closed
            assert not client.is_closed
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize("authenticate_first", [False, True])
def test_digest_authentication_survives_same_origin_redirect(
    tmp_path: Path, authenticate_first: bool
) -> None:
    authenticated_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        if (
            authenticate_first or request.url.path == "/protected"
        ) and not authorization:
            return httpx.Response(
                401, headers={"WWW-Authenticate": CHALLENGE}, stream=Body((b"auth",))
            )
        if authorization:
            assert f'uri="{request.url.path}"' in authorization
            authenticated_paths.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": "/protected"}, stream=Body(())
            )
        return httpx.Response(200, stream=Body((b"protected",)))

    async def run() -> None:
        async with (
            httpx.AsyncClient(
                auth=httpx.DigestAuth("user", "pass"),
                transport=httpx.MockTransport(handle),
            ) as client,
            AsyncHttpSourceResolver(
                client, policy=lambda source: True, temp_dir=tmp_path
            ).resolve(HttpSource("https://example.org/start")) as resource,
        ):
            assert resource.path.read_bytes() == b"protected"
        assert authenticated_paths[-1] == "/protected"
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_digest_budget_failure_survives_stream_close_failure(tmp_path: Path) -> None:
    class BrokenClose(Body):
        async def aclose(self) -> None:
            self.closed = True
            raise OSError("close failed")

    body = BrokenClose((b"too large",))

    async def run() -> None:
        async with httpx.AsyncClient(
            auth=httpx.DigestAuth("user", "pass"),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401, headers={"WWW-Authenticate": CHALLENGE}, stream=body
                )
            ),
        ) as client:
            with pytest.raises(SourceResourceLimitExceeded) as captured:
                async with AsyncHttpSourceResolver(
                    client, policy=lambda source: True, max_bytes=4, temp_dir=tmp_path
                ).resolve(HttpSource("https://example.org/protected")):
                    pytest.fail("oversize challenge delivered")
            assert any("close failed" in note for note in captured.value.__notes__)
        assert body.closed and list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_http_failure_survives_repeated_cancellation_during_close(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        closing = asyncio.Event()
        release = asyncio.Event()

        class BlockingClose(Body):
            async def aclose(self) -> None:
                closing.set()
                await release.wait()
                self.closed = True

        body = BlockingClose(())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, stream=body)
            )
        ) as client:

            async def enter() -> None:
                async with AsyncHttpSourceResolver(
                    client, policy=lambda source: True, temp_dir=tmp_path
                ).resolve(HttpSource("https://example.org/file")):
                    pytest.fail("HTTP failure delivered")

            task = asyncio.create_task(enter())
            try:
                await asyncio.wait_for(closing.wait(), 3)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                release.set()
                with pytest.raises(SourceError, match="status 500"):
                    await task
                assert body.closed and not client.is_closed
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_repeated_cancellation_waits_for_http_stream_close(tmp_path: Path) -> None:
    async def run() -> None:
        reading = asyncio.Event()
        closing = asyncio.Event()
        release = asyncio.Event()

        class BlockingBody(Body):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b"part"
                reading.set()
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                closing.set()
                await release.wait()
                self.closed = True

        body = BlockingBody(())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=body)
            )
        ) as client:
            resolver = AsyncHttpSourceResolver(
                client, policy=lambda source: True, temp_dir=tmp_path
            )

            async def enter() -> None:
                async with resolver.resolve(HttpSource("https://example.org/file")):
                    pytest.fail("cancelled download delivered")

            task = asyncio.create_task(enter())
            try:
                await asyncio.wait_for(reading.wait(), 3)
                task.cancel()
                await asyncio.wait_for(closing.wait(), 3)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert body.closed
                assert not client.is_closed
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())
