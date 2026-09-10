from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.engines.loaders import LocalLoader, URLLoader
from comet_rag.infrastructure.loaders import S3Loader
from comet_rag.ports import SourceLoaderPort
from tests.contracts.source_loader import SourceLoaderContract

PAYLOAD = b"shared loader contract"


class TestLocalLoaderContract(SourceLoaderContract):
    @pytest.fixture
    def loader(self) -> LocalLoader:
        return LocalLoader()

    @pytest.fixture
    def sources(self, tmp_path: Path) -> list[str]:
        paths = [tmp_path / "first.txt", tmp_path / "second.txt"]
        for path in paths:
            path.write_bytes(PAYLOAD)
        return [str(path) for path in paths]

    @pytest.fixture
    def expected_payload(self) -> bytes:
        return PAYLOAD

    @pytest.fixture
    def expected_temporary(self) -> bool:
        return False


class TestURLLoaderContract(SourceLoaderContract):
    @pytest.fixture
    async def loader(self, tmp_path: Path, monkeypatch) -> AsyncIterator[URLLoader]:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=PAYLOAD, request=request)

        transport = httpx.MockTransport(handler)
        sync_client = httpx.Client(transport=transport)
        async_client = httpx.AsyncClient(transport=transport)
        loader = URLLoader(
            download_dir=tmp_path,
            client=sync_client,
            async_client=async_client,
        )
        monkeypatch.setattr(
            "comet_rag.engines.loaders.url_loader.detect_content_type_from_path",
            lambda path: "txt",
        )
        yield loader
        await loader.acleanup()
        sync_client.close()
        await async_client.aclose()

    @pytest.fixture
    def sources(self) -> list[str]:
        return [
            "https://example.invalid/first.txt",
            "https://example.invalid/second.txt",
        ]

    @pytest.fixture
    def expected_payload(self) -> bytes:
        return PAYLOAD

    @pytest.fixture
    def expected_temporary(self) -> bool:
        return True


class _SyncBody:
    def iter_chunks(self, *, chunk_size: int):
        assert chunk_size > 0
        yield PAYLOAD

    def close(self) -> None:
        return None


class _AsyncBody:
    async def iter_chunks(self, *, chunk_size: int):
        assert chunk_size > 0
        yield PAYLOAD

    async def close(self) -> None:
        return None


class _SyncS3Client:
    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"ContentLength": len(PAYLOAD), "ETag": '"contract"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"ContentLength": len(PAYLOAD), "Body": _SyncBody()}

    def close(self) -> None:
        return None


class _AsyncS3Client:
    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"ContentLength": len(PAYLOAD), "ETag": '"contract"'}

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"ContentLength": len(PAYLOAD), "Body": _AsyncBody()}

    async def close(self) -> None:
        return None


class TestS3LoaderContract(SourceLoaderContract):
    @pytest.fixture
    async def loader(self, tmp_path: Path, monkeypatch) -> AsyncIterator[S3Loader]:
        sync_client = _SyncS3Client()
        async_client = _AsyncS3Client()
        loader = S3Loader(
            download_dir=tmp_path,
            client=sync_client,
            async_client=async_client,
        )
        monkeypatch.setattr(
            "comet_rag.infrastructure.loaders.s3_loader.detect_content_type_from_path",
            lambda path: "txt",
        )
        yield loader
        await loader.acleanup()

    @pytest.fixture
    def sources(self) -> list[str]:
        return [
            "s3://documents/first.txt",
            "s3://documents/second.txt",
        ]

    @pytest.fixture
    def expected_payload(self) -> bytes:
        return PAYLOAD

    @pytest.fixture
    def expected_temporary(self) -> bool:
        return True


def test_legacy_loader_content_is_runtime_alias() -> None:
    from comet_rag.engines.loaders import LoaderContent
    from comet_rag.ports import LoadedResource

    assert LoaderContent is LoadedResource


def test_port_rejects_an_object_without_batch_or_cleanup() -> None:
    class IncompleteLoader:
        def load(self, source: str):
            return source

        async def aload(self, source: str):
            return source

    assert not isinstance(IncompleteLoader(), SourceLoaderPort)


def test_contract_guard_detects_a_leaked_temporary_file(tmp_path: Path) -> None:
    leaked = tmp_path / "leaked.txt"
    leaked.write_bytes(PAYLOAD)

    with pytest.raises(AssertionError):
        SourceLoaderContract._assert_released(leaked, temporary=True)
