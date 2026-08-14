from __future__ import annotations

import inspect
from pathlib import Path

from comet_rag.engines.loaders.auto_loader import AutoLoader, LoaderRoute
from comet_rag.engines.loaders.base_loader import (
    DEFAULT_MAX_CONCURRENCY,
    BaseLoader,
)
from comet_rag.engines.loaders.data_type import (
    BaseFileFormat,
    CodeFormat,
    ContentStructure,
    MixedFormat,
    is_allowed_extension,
)
from comet_rag.engines.loaders.types import LoaderContent, SourceContent


class RecordingLoader(BaseLoader):
    def __init__(self, name: str) -> None:
        self.name = name
        self.batch_limits: list[int] = []
        self.async_batch_limits: list[int] = []
        self.cleanup_calls = 0
        self.async_cleanup_calls = 0

    def _result(self, source: SourceContent | str) -> LoaderContent:
        normalized = source if isinstance(source, SourceContent) else SourceContent(source)
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


def _scheme_route(scheme: str, loader: BaseLoader) -> LoaderRoute:
    return LoaderRoute(
        name=scheme,
        matcher=lambda source: source.parsed_url.scheme == scheme,
        loader=loader,
    )


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


def test_custom_minio_route_does_not_require_a_new_source_type() -> None:
    minio = RecordingLoader("minio")
    loader = AutoLoader(routes=[])
    loader.register_loader(
        "minio",
        minio,
        lambda source: source.parsed_url.scheme in {"s3", "minio"},
    )

    result = loader.load("s3://documents/report.pdf")

    assert result.metadata["loader"] == "minio"
    assert result.source.source == "s3://documents/report.pdf"


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


async def test_auto_loader_uses_specialized_async_batch_and_cleans_shared_once() -> None:
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
