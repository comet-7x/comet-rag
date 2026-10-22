from __future__ import annotations

from pathlib import Path

import pytest

from comet_rag.ports import AsyncSourceLoaderPort, LoadedResource, SourceLoaderPort


class _SourceExpectations:
    @pytest.fixture
    def sources(self) -> list[str]:  # pragma: no cover - 实现方提供
        raise NotImplementedError

    @pytest.fixture
    def expected_payload(self) -> bytes:  # pragma: no cover - 实现方提供
        raise NotImplementedError

    @pytest.fixture
    def expected_file_type(self) -> str:
        return "txt"

    @pytest.fixture
    def expected_temporary(self) -> bool:  # pragma: no cover - 实现方提供
        raise NotImplementedError

    @staticmethod
    def _assert_loaded(
        resource: LoadedResource,
        *,
        source: str,
        payload: bytes,
        file_type: str,
        temporary: bool,
    ) -> Path:
        path = resource.path
        assert path.is_file()
        assert path.read_bytes() == payload
        assert resource.source.source == source
        assert resource.file_type == file_type
        assert resource.file_size == len(payload)
        assert resource.is_temp is temporary
        return path

    @staticmethod
    def _assert_released(path: Path, *, temporary: bool) -> None:
        assert path.exists() is not temporary


class SourceLoaderContract(_SourceExpectations):
    @pytest.fixture
    def loader(self) -> SourceLoaderPort:  # pragma: no cover - 实现方提供
        raise NotImplementedError

    def test_satisfies_source_loader_port(self, loader: SourceLoaderPort) -> None:
        assert isinstance(loader, SourceLoaderPort)

    def test_sync_load_returns_managed_resource(
        self,
        loader: SourceLoaderPort,
        sources: list[str],
        expected_payload: bytes,
        expected_file_type: str,
        expected_temporary: bool,
    ) -> None:
        resource = loader.load(sources[0])
        path = self._assert_loaded(
            resource,
            source=sources[0],
            payload=expected_payload,
            file_type=expected_file_type,
            temporary=expected_temporary,
        )

        resource.cleanup()
        resource.cleanup()
        self._assert_released(path, temporary=expected_temporary)

    def test_sync_batch_preserves_source_order(
        self,
        loader: SourceLoaderPort,
        sources: list[str],
    ) -> None:
        resources = loader.batch_load(sources, max_concurrency=2)
        try:
            assert [resource.source.source for resource in resources] == sources
        finally:
            for resource in resources:
                resource.cleanup()

    def test_cleanup_is_idempotent(self, loader: SourceLoaderPort) -> None:
        loader.cleanup()
        loader.cleanup()
        loader.close()
        loader.close()


class AsyncSourceLoaderContract(_SourceExpectations):
    @pytest.fixture
    def async_loader(self) -> AsyncSourceLoaderPort:  # pragma: no cover - 实现方提供
        raise NotImplementedError

    async def test_async_load_matches_sync_semantics(
        self,
        async_loader: AsyncSourceLoaderPort,
        sources: list[str],
        expected_payload: bytes,
        expected_file_type: str,
        expected_temporary: bool,
    ) -> None:
        resource = await async_loader.load(sources[0])
        path = self._assert_loaded(
            resource,
            source=sources[0],
            payload=expected_payload,
            file_type=expected_file_type,
            temporary=expected_temporary,
        )

        resource.cleanup()
        resource.cleanup()
        self._assert_released(path, temporary=expected_temporary)

    async def test_async_batch_preserves_source_order(
        self,
        async_loader: AsyncSourceLoaderPort,
        sources: list[str],
    ) -> None:
        resources = await async_loader.batch_load(sources, max_concurrency=2)
        try:
            assert [resource.source.source for resource in resources] == sources
        finally:
            for resource in resources:
                resource.cleanup()

    def test_satisfies_async_source_loader_port(
        self, async_loader: AsyncSourceLoaderPort
    ) -> None:
        assert isinstance(async_loader, AsyncSourceLoaderPort)

    async def test_async_cleanup_is_idempotent(
        self, async_loader: AsyncSourceLoaderPort
    ) -> None:
        await async_loader.cleanup()
        await async_loader.cleanup()
        await async_loader.close()
        await async_loader.close()
