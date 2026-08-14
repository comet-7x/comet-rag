"""`URLLoader` 的连接复用与 client 所有权（spec S4-4）。

修复前每次下载都 `with httpx.Client(...)` 现建现销：重建连接池 + 重新
TLS 握手。批量入库大量 URL 时这是纯浪费，而且完全不体现在功能测试上 ——
所以需要专门盯住"client 被创建了几次、被谁关闭"。

全程用 `httpx.MockTransport` 在传输层拦截，不打真实网络（spec §6 硬性要求）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.loaders.url_loader import (
    ContentTypeMismatch,
    DownloadRequestConfig,
    DownloadTooLarge,
    URLLoader,
)

URL = "https://example.invalid/doc.txt"
BODY = b"hello from the network"


def _wait_for_cleanup_start(loader: URLLoader) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with loader._lifecycle:
            if loader._cleanup_in_progress:
                return
        time.sleep(0.001)
    raise TimeoutError("cleanup did not start")


def _transport(counter: dict[str, int] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter["requests"] = counter.get("requests", 0) + 1
        return httpx.Response(200, content=BODY)

    return httpx.MockTransport(handler)


@pytest.fixture
def loader(tmp_path: Path) -> Iterator[URLLoader]:
    ld = URLLoader(download_dir=tmp_path)
    yield ld
    ld.cleanup()


# ── 基本下载 ───────────────────────────────────────────────────────────────


def test_download_writes_temp_file(tmp_path: Path) -> None:
    ld = URLLoader(download_dir=tmp_path, client=httpx.Client(transport=_transport()))
    try:
        lc = ld.load(URL)
        assert lc.path.read_bytes() == BODY
        assert lc.is_temp is True
        assert lc.metadata["file_type"] == "txt"
    finally:
        ld.cleanup()


async def test_async_download_writes_temp_file(tmp_path: Path) -> None:
    ld = URLLoader(
        download_dir=tmp_path,
        async_client=httpx.AsyncClient(transport=_transport()),
    )
    try:
        lc = await ld.aload(URL)
        assert lc.path.read_bytes() == BODY
    finally:
        await ld.aclose()


def test_non_url_source_is_rejected(loader: URLLoader) -> None:
    with pytest.raises(ValueError, match="only handles URLs"):
        loader.load("/some/local/path")


def test_load_rejects_unsupported_options(loader: URLLoader) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        loader.load(URL, unsupported=True)  # type: ignore[call-arg]


async def test_aload_rejects_unsupported_options(loader: URLLoader) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await loader.aload(URL, unsupported=True)  # type: ignore[call-arg]


# ── 连接复用 ───────────────────────────────────────────────────────────────


def test_sync_client_is_created_once_and_reused(tmp_path: Path) -> None:
    """三次下载只应有一个 client 实例。"""
    ld = URLLoader(download_dir=tmp_path, client=httpx.Client(transport=_transport()))
    try:
        first = ld._shared_client()
        for _ in range(3):
            ld.load(URL)
        assert ld._shared_client() is first
    finally:
        ld.cleanup()


async def test_async_client_is_created_once_and_reused(tmp_path: Path) -> None:
    ld = URLLoader(
        download_dir=tmp_path,
        async_client=httpx.AsyncClient(transport=_transport()),
    )
    try:
        first = ld._shared_async_client()
        for _ in range(3):
            await ld.aload(URL)
        assert ld._shared_async_client() is first
    finally:
        await ld.aclose()


async def test_injected_client_is_used_for_every_request(tmp_path: Path) -> None:
    """注入的 client 必须真的承载请求，而不是被绕过去另建一个。"""
    counter: dict[str, int] = {}
    client = httpx.AsyncClient(transport=_transport(counter))
    ld = URLLoader(download_dir=tmp_path, async_client=client)
    try:
        for _ in range(4):
            await ld.aload(URL)
        assert counter["requests"] == 4
    finally:
        await ld.aclose()
        await client.aclose()


def test_sync_client_is_lazy(tmp_path: Path) -> None:
    """只用异步路径的调用方不该平白多一个同步连接池。"""
    ld = URLLoader(download_dir=tmp_path)
    try:
        assert ld._client is None
    finally:
        ld.cleanup()


# ── 所有权 ─────────────────────────────────────────────────────────────────


def test_cleanup_closes_self_made_client(tmp_path: Path) -> None:
    ld = URLLoader(download_dir=tmp_path)
    client = ld._shared_client()
    client._transport = _transport()  # 换掉传输层，避免真实网络

    ld.cleanup()

    assert client.is_closed
    assert ld._client is None


def test_cleanup_does_not_close_injected_client(tmp_path: Path) -> None:
    """关掉别人注入的 client 会连带影响调用方其余还在用它的代码。"""
    injected = httpx.Client(transport=_transport())
    ld = URLLoader(download_dir=tmp_path, client=injected)

    ld.load(URL)
    ld.cleanup()

    assert not injected.is_closed
    injected.close()


async def test_aclose_does_not_close_injected_async_client(tmp_path: Path) -> None:
    injected = httpx.AsyncClient(transport=_transport())
    ld = URLLoader(download_dir=tmp_path, async_client=injected)

    await ld.aload(URL)
    await ld.aclose()

    assert not injected.is_closed
    await injected.aclose()


async def test_aclose_closes_self_made_async_client(tmp_path: Path) -> None:
    ld = URLLoader(download_dir=tmp_path)
    client = ld._shared_async_client()
    client._transport = _transport()

    await ld.aclose()

    assert client.is_closed
    assert ld._async_client is None


# ── 临时文件清理 ───────────────────────────────────────────────────────────


def test_cleanup_removes_temp_files(tmp_path: Path) -> None:
    ld = URLLoader(download_dir=tmp_path, client=httpx.Client(transport=_transport()))
    ld.load(URL)
    ld.load(URL)
    paths = [Path(p) for p in ld.temp_files]
    assert all(p.exists() for p in paths)

    ld.cleanup()

    assert not any(p.exists() for p in paths)
    assert ld.temp_files == []


# ── 批量 ───────────────────────────────────────────────────────────────────


def test_batch_load_rejects_unsupported_options(loader: URLLoader) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        loader.batch_load([], unsupported=True)  # type: ignore[call-arg]


async def test_abatch_load_rejects_unsupported_options(loader: URLLoader) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await loader.abatch_load([], unsupported=True)  # type: ignore[call-arg]


async def test_abatch_load_shares_one_client(tmp_path: Path) -> None:
    """批量下载不该每个 URL 建一个连接池。"""
    counter: dict[str, int] = {}
    ld = URLLoader(
        download_dir=tmp_path,
        async_client=httpx.AsyncClient(transport=_transport(counter)),
    )
    try:
        before = ld._shared_async_client()
        results = await ld.abatch_load([URL] * 5, max_concurrency=3)

        assert len(results) == 5
        assert counter["requests"] == 5
        assert ld._shared_async_client() is before
    finally:
        await ld.aclose()


async def test_acleanup_waits_for_active_async_batch(
    tmp_path: Path, monkeypatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    ld = URLLoader(download_dir=tmp_path)

    async def delayed_aload_impl(
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> LoaderContent:
        assert client is not None
        started.set()
        await release.wait()
        normalized = source if isinstance(source, SourceContent) else SourceContent(source)
        return LoaderContent(path=tmp_path / "download.txt", source=normalized)

    monkeypatch.setattr(ld, "_aload_impl", delayed_aload_impl)
    batch_task = asyncio.create_task(ld.abatch_load([URL]))
    await started.wait()
    client = ld._async_client
    assert client is not None

    cleanup_task = asyncio.create_task(ld.acleanup())
    await asyncio.sleep(0)
    assert not cleanup_task.done()
    assert not client.is_closed

    release.set()
    await batch_task
    await cleanup_task

    assert client.is_closed
    assert ld._async_client is None


async def test_cleanup_waits_for_active_async_load(tmp_path: Path, monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    ld = URLLoader(download_dir=tmp_path)
    active_path = tmp_path / "active.txt"

    async def delayed_aload_impl(
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> LoaderContent:
        active_path.write_bytes(BODY)
        ld._temp_files.append(str(active_path))
        started.set()
        await release.wait()
        assert active_path.exists()
        normalized = source if isinstance(source, SourceContent) else SourceContent(source)
        return LoaderContent(path=active_path, source=normalized, is_temp=True)

    monkeypatch.setattr(ld, "_aload_impl", delayed_aload_impl)
    load_task = asyncio.create_task(ld.aload(URL))
    await started.wait()

    cleanup_task = asyncio.create_task(asyncio.to_thread(ld.cleanup))
    await asyncio.to_thread(_wait_for_cleanup_start, ld)
    assert not cleanup_task.done()
    assert active_path.exists()

    release.set()
    await load_task
    await cleanup_task

    assert not active_path.exists()
    assert ld.temp_files == []


@pytest.mark.parametrize("operation", ["load", "batch_load"])
def test_cleanup_waits_for_active_sync_load(
    operation: str, tmp_path: Path, monkeypatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    ld = URLLoader(download_dir=tmp_path)
    active_path = tmp_path / "active.txt"

    def delayed_load_impl(
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.Client | None = None,
    ) -> LoaderContent:
        active_path.write_bytes(BODY)
        ld._temp_files.append(str(active_path))
        started.set()
        assert release.wait(timeout=2)
        assert active_path.exists()
        normalized = source if isinstance(source, SourceContent) else SourceContent(source)
        return LoaderContent(path=active_path, source=normalized, is_temp=True)

    monkeypatch.setattr(ld, "_load_impl", delayed_load_impl)

    with ThreadPoolExecutor(max_workers=2) as executor:
        if operation == "load":
            load_future = executor.submit(ld.load, URL)
        else:
            load_future = executor.submit(ld.batch_load, [URL])
        assert started.wait(timeout=2)

        cleanup_future = executor.submit(ld.cleanup)
        _wait_for_cleanup_start(ld)
        assert not cleanup_future.done()
        assert active_path.exists()

        release.set()
        load_future.result(timeout=2)
        cleanup_future.result(timeout=2)

    assert not active_path.exists()
    assert ld.temp_files == []


def test_batch_load_shares_one_client(tmp_path: Path) -> None:
    counter: dict[str, int] = {}
    ld = URLLoader(
        download_dir=tmp_path, client=httpx.Client(transport=_transport(counter))
    )
    try:
        before = ld._shared_client()
        results = ld.batch_load([URL] * 5, max_concurrency=3)

        assert len(results) == 5
        assert counter["requests"] == 5
        assert ld._shared_client() is before
    finally:
        ld.cleanup()


def test_batch_load_uses_loader_request_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    client = httpx.Client(transport=_transport())
    ld = URLLoader(
        download_dir=tmp_path,
        client=client,
        timeout=17,
        follow_redirects=False,
    )
    captured: dict[str, DownloadRequestConfig] = {}

    def capture(client, sources, config, max_concurrency, **kwargs):
        captured["config"] = config
        return []

    monkeypatch.setattr(ld, "_batch_load_with", capture)
    try:
        assert ld.batch_load([]) == []
        assert captured["config"].timeout == 17
        assert captured["config"].follow_redirects is False
    finally:
        ld.cleanup()
        client.close()


# ── 错误路径 ───────────────────────────────────────────────────────────────


def test_http_error_preserves_httpx_type_for_retry_classification(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    ld = URLLoader(
        download_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            ld.load(URL)
    finally:
        ld.cleanup()


def test_content_length_over_limit_is_rejected_before_buffering(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "1000"}, content=b"x")

    ld = URLLoader(
        download_dir=tmp_path,
        max_download_bytes=10,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(DownloadTooLarge, match="declares 1000 bytes"):
            ld.load(URL)
        assert list(tmp_path.iterdir()) == []
    finally:
        ld.cleanup()


async def test_actual_stream_size_is_bounded_when_header_lies(tmp_path: Path) -> None:
    body = b"x" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "1"}, content=body)

    ld = URLLoader(
        download_dir=tmp_path,
        max_download_bytes=16,
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(DownloadTooLarge, match="while streaming"):
            await ld.aload(URL)
        assert list(tmp_path.iterdir()) == []
    finally:
        await ld.aclose()


async def test_redirect_target_is_validated_before_second_request(
    tmp_path: Path,
) -> None:
    requested: list[str] = []
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    def reject_private(url: str) -> None:
        validated.append(url)
        raise PermissionError("private redirect rejected")

    ld = URLLoader(
        download_dir=tmp_path,
        redirect_validator=reject_private,
        async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(PermissionError, match="private redirect"):
            await ld.aload(URL)
        assert requested == [URL]
        assert validated == ["http://127.0.0.1/secret"]
    finally:
        await ld.aclose()


def test_empty_body_is_rejected(tmp_path: Path) -> None:
    """空文件下游一定会炸，不如在这里就说清楚原因。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    ld = URLLoader(
        download_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(ValueError, match="empty"):
            ld.load(URL)
    finally:
        ld.cleanup()


def test_content_probe_rejects_html_body_behind_docx_suffix(
    tmp_path: Path, monkeypatch
) -> None:
    """可信 URL 后缀也不能覆盖实际字节；否则登录页会下沉成解析器错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>login required</html>")

    monkeypatch.setattr(
        "comet_rag.engines.loaders.url_loader.detect_content_type_from_path",
        lambda path: "html",
    )
    ld = URLLoader(
        download_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(ContentTypeMismatch, match="docx.*html"):
            ld.load("https://example.invalid/report.docx")
        assert list(tmp_path.iterdir()) == []
    finally:
        ld.cleanup()
