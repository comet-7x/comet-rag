from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from comet_rag.infrastructure.loaders.s3_loader import (
    ObjectContentTypeMismatch,
    ObjectTooLarge,
    S3Loader,
    parse_s3_uri,
)

BODY = b"hello from object storage"
URI = "s3://documents/reports/annual.txt"


class SyncBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    def iter_chunks(self, *, chunk_size: int):
        assert chunk_size > 0
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class AsyncBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def iter_chunks(self, *, chunk_size: int):
        assert chunk_size > 0
        for chunk in self.chunks:
            yield chunk

    def close(self) -> None:
        self.closed = True


class SyncClient:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        head_size: int | None = None,
        get_size: int | None = None,
    ) -> None:
        self.chunks = chunks or [BODY]
        self.head_size = len(BODY) if head_size is None else head_size
        self.get_size = sum(map(len, self.chunks)) if get_size is None else get_size
        self.head_calls = 0
        self.get_calls = 0
        self.closed = False
        self.body: SyncBody | None = None

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Bucket": "documents", "Key": "reports/annual.txt"}
        self.head_calls += 1
        return {"ContentLength": self.head_size, "ETag": '"etag-value"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Bucket": "documents", "Key": "reports/annual.txt"}
        self.get_calls += 1
        self.body = SyncBody(self.chunks)
        return {"ContentLength": self.get_size, "Body": self.body}

    def close(self) -> None:
        self.closed = True


class AsyncClient:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        head_size: int | None = None,
        get_size: int | None = None,
        body: AsyncBody | None = None,
    ) -> None:
        self.chunks = chunks or [BODY]
        self.head_size = len(BODY) if head_size is None else head_size
        self.get_size = sum(map(len, self.chunks)) if get_size is None else get_size
        self.head_calls = 0
        self.get_calls = 0
        self.closed = False
        self.body = body

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Bucket": "documents", "Key": "reports/annual.txt"}
        self.head_calls += 1
        return {"ContentLength": self.head_size, "ETag": '"async-etag"'}

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Bucket": "documents", "Key": "reports/annual.txt"}
        self.get_calls += 1
        if self.body is None:
            self.body = AsyncBody(self.chunks)
        return {"ContentLength": self.get_size, "Body": self.body}

    async def close(self) -> None:
        self.closed = True


class AsyncClientContext:
    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> AsyncClient:
        self.entered += 1
        return self.client

    async def __aexit__(self, *_: object) -> None:
        self.exited += 1
        await self.client.close()


@pytest.fixture(autouse=True)
def _detect_text(monkeypatch) -> None:
    monkeypatch.setattr(
        "comet_rag.infrastructure.loaders.s3_loader.detect_content_type_from_path",
        lambda path: "txt",
    )


def test_parse_s3_and_minio_uris() -> None:
    assert parse_s3_uri(URI).bucket == "documents"
    assert parse_s3_uri(URI).key == "reports/annual.txt"
    assert parse_s3_uri("minio://documents/a%20b.txt").key == "a b.txt"


@pytest.mark.parametrize(
    "source",
    [
        "https://example.com/a.txt",
        "s3:///a.txt",
        "s3://documents",
        "s3://user:secret@documents/a.txt",
        "s3://documents:9000/a.txt",
        "s3://documents/a.txt?versionId=1",
    ],
)
def test_invalid_object_uris_are_rejected(source: str) -> None:
    with pytest.raises(ValueError):
        parse_s3_uri(source)


def test_sync_load_downloads_metadata_and_cleans_temp_file(tmp_path: Path) -> None:
    client = SyncClient()
    loader = S3Loader(download_dir=tmp_path, client=client)

    content = loader.load(URI)

    assert content.path.read_bytes() == BODY
    assert content.is_temp is True
    assert content.metadata == {
        "source_type": "s3",
        "bucket": "documents",
        "object_key": "reports/annual.txt",
        "file_name": "annual.txt",
        "file_type": "txt",
        "file_size": len(BODY),
        "etag": "etag-value",
        "parse_config": content.metadata["parse_config"],
    }
    assert client.body is not None and client.body.closed

    loader.cleanup()

    assert not content.path.exists()
    assert not client.closed, "injected clients remain caller-owned"


async def test_async_load_reuses_client_and_cleans_temp_files(tmp_path: Path) -> None:
    client = AsyncClient()
    loader = S3Loader(download_dir=tmp_path, async_client=client)

    first = await loader.aload(URI)
    second = await loader.aload(URI)

    assert first.path.read_bytes() == BODY
    assert second.path.read_bytes() == BODY
    assert client.head_calls == 2
    assert client.get_calls == 2

    await loader.acleanup()

    assert not first.path.exists()
    assert not second.path.exists()
    assert not client.closed, "injected clients remain caller-owned"


def test_head_size_limit_rejects_before_get_object(tmp_path: Path) -> None:
    client = SyncClient(head_size=11)
    loader = S3Loader(download_dir=tmp_path, client=client, max_object_bytes=10)

    with pytest.raises(ObjectTooLarge, match="head_object"):
        loader.load(URI)

    assert client.get_calls == 0
    assert list(tmp_path.iterdir()) == []


def test_stream_size_limit_catches_stale_metadata(tmp_path: Path) -> None:
    client = SyncClient([b"abc", b"def"], head_size=1, get_size=1)
    loader = S3Loader(download_dir=tmp_path, client=client, max_object_bytes=5)

    with pytest.raises(ObjectTooLarge, match="while streaming"):
        loader.load(URI)

    assert loader.temp_files == []
    assert list(tmp_path.iterdir()) == []


def test_get_object_size_rejection_still_closes_response_body(tmp_path: Path) -> None:
    client = SyncClient(head_size=1, get_size=100)
    loader = S3Loader(download_dir=tmp_path, client=client, max_object_bytes=10)

    with pytest.raises(ObjectTooLarge, match="get_object"):
        loader.load(URI)

    assert client.body is not None and client.body.closed
    assert list(tmp_path.iterdir()) == []


def test_content_mismatch_is_rejected_and_temp_removed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "comet_rag.infrastructure.loaders.s3_loader.detect_content_type_from_path",
        lambda path: "html",
    )
    loader = S3Loader(download_dir=tmp_path, client=SyncClient())

    with pytest.raises(ObjectContentTypeMismatch, match="txt.*html"):
        loader.load(URI)

    assert loader.temp_files == []
    assert list(tmp_path.iterdir()) == []


def test_owned_sync_client_is_lazy_reused_and_closed(
    tmp_path: Path, monkeypatch
) -> None:
    client = SyncClient()
    loader = S3Loader(download_dir=tmp_path)
    monkeypatch.setattr(loader, "_new_sync_client", lambda: client)

    loader.load(URI)
    loader.load(URI)
    loader.cleanup()

    assert client.head_calls == 2
    assert client.closed
    assert loader._client is None


async def test_owned_async_client_context_is_reused_and_closed(
    tmp_path: Path, monkeypatch
) -> None:
    client = AsyncClient()
    context = AsyncClientContext(client)
    loader = S3Loader(download_dir=tmp_path)
    monkeypatch.setattr(loader, "_new_async_client_context", lambda: context)

    await loader.aload(URI)
    await loader.aload(URI)
    await loader.acleanup()

    assert context.entered == 1
    assert context.exited == 1
    assert client.closed
    assert loader._async_client is None


class DelayedAsyncBody(AsyncBody):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__([BODY])
        self.started = started
        self.release = release

    async def iter_chunks(self, *, chunk_size: int):
        self.started.set()
        await self.release.wait()
        async for chunk in super().iter_chunks(chunk_size=chunk_size):
            yield chunk


async def test_acleanup_waits_for_active_download(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    body = DelayedAsyncBody(started, release)
    loader = S3Loader(
        download_dir=tmp_path,
        async_client=AsyncClient(body=body),
    )

    load_task = asyncio.create_task(loader.aload(URI))
    await started.wait()
    cleanup_task = asyncio.create_task(loader.acleanup())
    await asyncio.sleep(0)

    assert not cleanup_task.done()
    assert loader.temp_files and Path(loader.temp_files[0]).exists()

    release.set()
    content = await load_task
    await cleanup_task

    assert not content.path.exists()
    assert loader.temp_files == []
