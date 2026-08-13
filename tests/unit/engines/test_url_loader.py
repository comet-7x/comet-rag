"""`URLLoader` 的连接复用与 client 所有权（spec S4-4）。

修复前每次下载都 `with httpx.Client(...)` 现建现销：重建连接池 + 重新
TLS 握手。批量入库大量 URL 时这是纯浪费，而且完全不体现在功能测试上 ——
所以需要专门盯住"client 被创建了几次、被谁关闭"。

全程用 `httpx.MockTransport` 在传输层拦截，不打真实网络（spec §6 硬性要求）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from comet_rag.engines.loaders.url_loader import URLLoader

URL = "https://example.invalid/doc.txt"
BODY = b"hello from the network"


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


# ── 错误路径 ───────────────────────────────────────────────────────────────


def test_http_error_is_wrapped(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    ld = URLLoader(
        download_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(ValueError, match="Download failed"):
            ld.load(URL)
    finally:
        ld.cleanup()


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
