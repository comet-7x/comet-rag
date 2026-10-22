from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.infrastructure.extractors import (
    AsyncMinerUDocumentExtractor,
    MinerUDocumentExtractor,
)
from comet_rag.infrastructure.sources import (
    AsyncAutoLoader,
    AsyncBaseLoader,
    AsyncLocalLoader,
    AsyncURLLoader,
    AutoLoader,
    BaseLoader,
    LocalLoader,
    URLLoader,
)
from comet_rag.infrastructure.sources.s3 import AsyncS3Loader, S3Loader
from comet_rag.ports.source import LoadedResource, SourceContent


@pytest.mark.parametrize(
    ("sync_type", "async_type"),
    [
        (BaseLoader, AsyncBaseLoader),
        (LocalLoader, AsyncLocalLoader),
        (URLLoader, AsyncURLLoader),
        (S3Loader, AsyncS3Loader),
        (AutoLoader, AsyncAutoLoader),
    ],
)
def test_loader_types_have_one_execution_model(
    sync_type: type, async_type: type
) -> None:
    for name in ("load", "batch_load", "cleanup", "close"):
        assert not inspect.iscoroutinefunction(getattr(sync_type, name))
        assert inspect.iscoroutinefunction(getattr(async_type, name))
    for old_name in ("aload", "abatch_load", "acleanup", "aclose"):
        assert not hasattr(sync_type, old_name)
        assert not hasattr(async_type, old_name)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_mineru_constructs_only_its_execution_models_client(
    asynchronous: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("opposite execution model client was constructed")

    if asynchronous:
        monkeypatch.setattr(httpx, "Client", forbidden_client)
        extractor = AsyncMinerUDocumentExtractor("https://mineru.test")
        assert inspect.iscoroutinefunction(extractor.extract)
        await extractor.close()
    else:
        monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
        sync_extractor = MinerUDocumentExtractor("https://mineru.test")
        assert not inspect.iscoroutinefunction(sync_extractor.extract)
        sync_extractor.close()


class _S3Body:
    async def iter_chunks(self, **_: Any):
        yield b"source payload"

    async def close(self) -> None:
        pass


class _S3Client:
    async def head_object(self, **_: Any) -> dict[str, Any]:
        return {"ContentLength": 14}

    async def get_object(self, **_: Any) -> dict[str, Any]:
        return {"ContentLength": 14, "Body": _S3Body()}


class _BlockingFile:
    def __init__(
        self, file: Any, entered: threading.Event, release: threading.Event
    ) -> None:
        self._file = file
        self.name = file.name
        self._entered = entered
        self._release = release
        self.written = False

    def write(self, data: bytes) -> int:
        self._entered.set()
        assert self._release.wait(2), "test did not release the file writer"
        assert Path(self.name).exists(), (
            "cleanup removed a file while its writer was active"
        )
        count = self._file.write(data)
        self.written = True
        return count

    def close(self) -> None:
        self._file.close()


@pytest.mark.parametrize("backend", ["http", "s3"])
@pytest.mark.parametrize("stage", ["write", "finalize"])
async def test_cancellation_waits_for_file_work_before_cleanup(
    backend: str, stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "comet_rag.infrastructure.sources.file_info.detect_content_type_from_path",
        lambda path: "txt",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"source payload")
        )
    )
    loader: Any = (
        AsyncURLLoader(download_dir=tmp_path, client=client)
        if backend == "http"
        else AsyncS3Loader(download_dir=tmp_path, client=_S3Client())
    )
    source = (
        "https://example.test/document.txt"
        if backend == "http"
        else "s3://documents/file.txt"
    )
    entered = threading.Event()
    release = threading.Event()
    files: list[_BlockingFile] = []
    original_create = loader._new_temp_file
    original_finalize = loader._finalize_temp

    def create_file() -> _BlockingFile:
        file = _BlockingFile(original_create(), entered, release)
        files.append(file)
        return file

    def finalize(path: str, name: Any) -> str:
        entered.set()
        assert release.wait(2), "test did not release content detection"
        assert Path(path).exists()
        return original_finalize(path, name)

    if stage == "write":
        monkeypatch.setattr(loader, "_new_temp_file", create_file)
    else:
        monkeypatch.setattr(loader, "_finalize_temp", finalize)
    task = asyncio.create_task(loader.load(source))
    cleanup: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        cleanup = asyncio.create_task(loader.cleanup())
        await asyncio.sleep(0.02)
        assert not task.done(), (
            "cancelled load returned while its file worker was running"
        )
        assert not cleanup.done(), "cleanup crossed the active file work boundary"
        assert len(list(tmp_path.iterdir())) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if cleanup is not None:
            await cleanup
        await loader.cleanup()
        await client.aclose()
    assert task.cancelled()
    if stage == "write":
        assert files[0].written
    assert loader.temp_files == []
    assert list(tmp_path.iterdir()) == []


async def test_failed_http_batch_drains_siblings_before_releasing_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = AsyncURLLoader()
    started = asyncio.Event()
    stopping = asyncio.Event()
    release = asyncio.Event()
    sibling: asyncio.Task[Any] | None = None

    async def load_one(source: SourceContent | str, **_: Any) -> LoadedResource:
        nonlocal sibling
        if source == "fail":
            await started.wait()
            raise RuntimeError("download failed")
        sibling = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()
        raise AssertionError("cancelled sibling must not return")

    monkeypatch.setattr(loader, "_load_impl", load_one)
    batch = asyncio.create_task(loader.batch_load(["fail", "slow"]))
    cleanup: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(2):
            await stopping.wait()
        cleanup = asyncio.create_task(loader.cleanup())
        await asyncio.sleep(0)
        assert not batch.done()
        assert not cleanup.done()
        assert loader._active_loads == 1
    finally:
        release.set()
        if sibling is not None and not sibling.done():
            sibling.cancel()
        results = await asyncio.gather(batch, return_exceptions=True)
        if cleanup is not None:
            await cleanup
        await loader.cleanup()
    assert isinstance(results[0], RuntimeError)
    assert str(results[0]) == "download failed"
    assert loader._active_loads == 0
