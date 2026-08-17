from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from comet_rag.engines.loaders.auto_loader import AutoLoader, LoaderRoute
from comet_rag.engines.loaders.base_loader import (
    DEFAULT_MAX_CONCURRENCY,
    BaseLoader,
)
from comet_rag.engines.loaders.data_type import (
    BaseFileFormat,
    CodeFormat,
    ContentStructure,
    ContentTypeMismatch,
    MixedFormat,
    UnsupportedContentType,
    is_allowed_extension,
    resolve_detected_extension,
)
from comet_rag.engines.loaders.local_loader import LocalLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent


class RecordingLoader(BaseLoader):
    def __init__(self, name: str) -> None:
        self.name = name
        self.batch_limits: list[int] = []
        self.async_batch_limits: list[int] = []
        self.cleanup_calls = 0
        self.async_cleanup_calls = 0

    def _result(self, source: SourceContent | str) -> LoaderContent:
        normalized = (
            source if isinstance(source, SourceContent) else SourceContent(source)
        )
        return LoaderContent(
            path=Path(normalized.parsed_url.path.lstrip("/") or "object"),
            source=normalized,
            metadata={"loader": self.name},
        )

    def load(self, source: SourceContent | str, *args, **kwargs) -> LoaderContent:
        return self._result(source)

    async def aload(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderContent:
        return self._result(source)

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        **kwargs,
    ) -> list[LoaderContent]:
        self.batch_limits.append(max_concurrency)
        return [self._result(source) for source in sources]

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        **kwargs,
    ) -> list[LoaderContent]:
        self.async_batch_limits.append(max_concurrency)
        return [self._result(source) for source in sources]

    def cleanup(self) -> None:
        self.cleanup_calls += 1

    async def acleanup(self) -> None:
        self.async_cleanup_calls += 1


class LegacyCloseLoader(BaseLoader):
    def __init__(self) -> None:
        self.sync_cleanup_calls = 0
        self.async_close_calls = 0

    def load(self, source: SourceContent | str) -> LoaderContent:
        normalized = (
            source if isinstance(source, SourceContent) else SourceContent(source)
        )
        return LoaderContent(path=Path("legacy"), source=normalized)

    async def aload(self, source: SourceContent | str) -> LoaderContent:
        return self.load(source)

    def cleanup(self) -> None:
        self.sync_cleanup_calls += 1

    async def aclose(self) -> None:
        self.async_close_calls += 1


def _scheme_route(scheme: str, loader: BaseLoader) -> LoaderRoute:
    return LoaderRoute.schemes(scheme, loader, {scheme})


def test_batch_concurrency_is_an_explicit_public_parameter() -> None:
    for method in (BaseLoader.batch_load, BaseLoader.abatch_load):
        parameter = inspect.signature(method).parameters["max_concurrency"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default == DEFAULT_MAX_CONCURRENCY


async def test_async_context_manager_uses_async_cleanup_contract() -> None:
    loader = RecordingLoader("async")

    async with loader:
        pass

    assert loader.async_cleanup_calls == 1
    assert loader.cleanup_calls == 0


async def test_auto_loader_preserves_legacy_async_close_contract() -> None:
    legacy = LegacyCloseLoader()
    loader = AutoLoader(routes=[_scheme_route("legacy", legacy)])

    await loader.acleanup()

    assert legacy.async_close_calls == 1
    assert legacy.sync_cleanup_calls == 0


async def test_local_loader_rejects_unsupported_options(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("content", encoding="utf-8")
    loader = LocalLoader()

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        loader.load(source, unsupported=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await loader.aload(source, unsupported=True)  # type: ignore[call-arg]


def test_custom_minio_route_uses_extensible_source_scheme() -> None:
    minio = RecordingLoader("minio")
    loader = AutoLoader([LoaderRoute.schemes("minio", minio, {"s3", "minio"})])

    result = loader.load("s3://documents/report.pdf")

    assert result.metadata["loader"] == "minio"
    assert result.source.source == "s3://documents/report.pdf"
    assert result.source.source_type == "s3"


def test_scheme_route_leaves_source_validation_to_concrete_loader() -> None:
    route = LoaderRoute.schemes("s3", RecordingLoader("s3"), {"s3", "minio"})

    assert route.matcher(SourceContent("s3:///missing-bucket.txt")) is True


def test_scheme_route_accepts_one_string_without_splitting_characters() -> None:
    route = LoaderRoute.schemes("secure-url", RecordingLoader("https"), "https")

    assert route.matcher(SourceContent("https://example.invalid/document.txt")) is True
    assert route.matcher(SourceContent("http://example.invalid/document.txt")) is False


def test_loader_route_preserves_legacy_positional_field_order() -> None:
    def matcher(source: SourceContent) -> bool:
        return source.parsed_url.scheme == "custom"

    loader = RecordingLoader("custom")

    route = LoaderRoute("custom", matcher, loader)

    assert route.matcher(SourceContent("custom://bucket/object.txt")) is True
    assert route.loader is loader


def test_constructor_rejects_duplicate_route_names() -> None:
    first = RecordingLoader("first")
    second = RecordingLoader("second")

    with pytest.raises(ValueError, match="route names must be unique"):
        AutoLoader(
            routes=[
                _scheme_route("duplicate", first),
                _scheme_route("duplicate", second),
            ]
        )


async def test_auto_loader_rejects_loader_specific_options_at_router_boundary() -> None:
    alpha = RecordingLoader("alpha")
    beta = RecordingLoader("beta")
    loader = AutoLoader(
        routes=[_scheme_route("alpha", alpha), _scheme_route("beta", beta)]
    )
    untyped_loader: Any = loader

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        untyped_loader.load("alpha://bucket/1", download_config=object())
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await untyped_loader.aload("alpha://bucket/1", download_config=object())
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        untyped_loader.batch_load(
            ["alpha://bucket/1", "beta://bucket/2"],
            download_config=object(),
        )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await untyped_loader.abatch_load(
            ["alpha://bucket/1", "beta://bucket/2"],
            download_config=object(),
        )

    assert alpha.batch_limits == []
    assert beta.batch_limits == []
    assert alpha.async_batch_limits == []
    assert beta.async_batch_limits == []


def test_auto_loader_groups_mixed_batch_and_restores_input_order() -> None:
    alpha = RecordingLoader("alpha")
    beta = RecordingLoader("beta")
    loader = AutoLoader(
        routes=[_scheme_route("alpha", alpha), _scheme_route("beta", beta)]
    )

    results = loader.batch_load(
        ["alpha://bucket/1", "beta://bucket/2", "alpha://bucket/3"],
        max_concurrency=4,
    )

    assert [result.metadata["loader"] for result in results] == [
        "alpha",
        "beta",
        "alpha",
    ]
    assert alpha.batch_limits == [4]
    assert beta.batch_limits == [4]


async def test_auto_loader_uses_specialized_async_batch_and_cleans_shared_once() -> (
    None
):
    shared = RecordingLoader("shared")
    loader = AutoLoader(
        routes=[_scheme_route("first", shared), _scheme_route("second", shared)]
    )

    results = await loader.abatch_load(
        ["first://bucket/1", "second://bucket/2"], max_concurrency=3
    )
    await loader.acleanup()

    assert [result.metadata["loader"] for result in results] == ["shared", "shared"]
    assert shared.async_batch_limits == [3]
    assert shared.async_cleanup_calls == 1


def test_file_format_registry_is_explicit_and_queryable() -> None:
    assert BaseFileFormat.from_extension(".DOCX") is MixedFormat
    assert BaseFileFormat.from_extension("py") is CodeFormat
    assert BaseFileFormat.all_by_structure(ContentStructure.CODE) == [CodeFormat]
    assert is_allowed_extension(".pdf") is True
    assert is_allowed_extension("exe") is False


def test_detected_extension_policy_is_shared_and_allow_ext_backed() -> None:
    assert resolve_detected_extension("py", "txt") == "py"
    assert resolve_detected_extension("", "pdf") == "pdf"
    with pytest.raises(ContentTypeMismatch, match="docx.*html"):
        resolve_detected_extension("docx", "html")
    with pytest.raises(UnsupportedContentType, match="zip"):
        resolve_detected_extension("docx", "zip")
