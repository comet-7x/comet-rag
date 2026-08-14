import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from comet_rag.engines.loaders.base_loader import (
    DEFAULT_MAX_CONCURRENCY,
    BaseLoader,
)
from comet_rag.engines.loaders.types import LoaderContent, SourceContent, SourceType

LoaderMatcher = Callable[[SourceContent], bool]


@dataclass(frozen=True, slots=True)
class LoaderRoute:
    """A named routing rule used by :class:`AutoLoader`.

    Matchers make the router extensible without adding every storage scheme to the
    closed ``SourceType`` enum. For example, an infrastructure-layer MinIO loader
    can register a matcher for ``s3://`` sources without engines importing its SDK.
    """

    name: str
    matcher: LoaderMatcher
    loader: BaseLoader

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("LoaderRoute.name must not be empty")


class AutoLoader(BaseLoader):
    """Route sources to loaders while preserving each loader's batch semantics.

    ``loaders`` keeps the original ``SourceType`` mapping API. ``routes`` is the
    extensible form for custom schemes such as S3/MinIO. They are mutually exclusive
    so route precedence remains explicit.
    """

    def __init__(
        self,
        loaders: Mapping[SourceType, BaseLoader] | None = None,
        download_dir: str | Path | None = None,
        max_download_bytes: int | None = None,
        redirect_validator: Callable[[str], None] | None = None,
        *,
        routes: Sequence[LoaderRoute] | None = None,
    ) -> None:
        if loaders is not None and routes is not None:
            raise ValueError("loaders and routes cannot be provided together")

        if routes is not None:
            self._routes = list(routes)
        elif loaders is not None:
            self._routes = [
                self._route_for_source_type(source_type, loader)
                for source_type, loader in loaders.items()
            ]
        else:
            from comet_rag.engines.loaders.local_loader import LocalLoader
            from comet_rag.engines.loaders.url_loader import (
                DEFAULT_MAX_DOWNLOAD_BYTES,
                URLLoader,
            )

            defaults: dict[SourceType, BaseLoader] = {
                SourceType.LOCAL: LocalLoader(),
                SourceType.URL: URLLoader(
                    download_dir=download_dir,
                    max_download_bytes=(
                        max_download_bytes
                        if max_download_bytes is not None
                        else DEFAULT_MAX_DOWNLOAD_BYTES
                    ),
                    redirect_validator=redirect_validator,
                ),
            }
            self._routes = [
                self._route_for_source_type(source_type, loader)
                for source_type, loader in defaults.items()
            ]

    @staticmethod
    def _route_for_source_type(
        source_type: SourceType, loader: BaseLoader
    ) -> LoaderRoute:
        return LoaderRoute(
            name=source_type.value,
            matcher=lambda source, expected=source_type: (
                source.pre_source_type is expected
            ),
            loader=loader,
        )

    def register_loader(
        self,
        name: str,
        loader: BaseLoader,
        matcher: LoaderMatcher,
        *,
        prepend: bool = False,
    ) -> None:
        """Register a custom route.

        Set ``prepend=True`` when the custom matcher should override a built-in
        route. Route names are unique to keep diagnostics and precedence clear.
        """

        if any(route.name == name for route in self._routes):
            raise ValueError(f"Loader route {name!r} is already registered")
        route = LoaderRoute(name=name, matcher=matcher, loader=loader)
        if prepend:
            self._routes.insert(0, route)
        else:
            self._routes.append(route)

    def _resolve_route(self, source: SourceContent) -> LoaderRoute:
        for route in self._routes:
            if route.matcher(source):
                return route
        raise ValueError(f"No loader route matched source: {source.source!r}")

    def _resolve(self, source: SourceContent) -> BaseLoader:
        """Compatibility helper returning only the matched loader."""

        return self._resolve_route(source).loader

    @staticmethod
    def _normalize_source(source: SourceContent | str) -> SourceContent:
        return source if isinstance(source, SourceContent) else SourceContent(source)

    def load(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderContent:
        normalized = self._normalize_source(source)
        return self._resolve(normalized).load(normalized, *args, **kwargs)

    async def aload(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderContent:
        normalized = self._normalize_source(source)
        # Route matching may inspect the local filesystem. Keep that blocking stat
        # off the event loop for local paths and custom network filesystems.
        loader = await asyncio.to_thread(self._resolve, normalized)
        return await loader.aload(normalized, *args, **kwargs)

    def _group_sources(
        self, sources: list[SourceContent] | list[str]
    ) -> list[tuple[BaseLoader, list[tuple[int, SourceContent]]]]:
        groups: dict[int, tuple[BaseLoader, list[tuple[int, SourceContent]]]] = {}
        for index, source in enumerate(sources):
            normalized = self._normalize_source(source)
            loader = self._resolve(normalized)
            group = groups.setdefault(id(loader), (loader, []))
            group[1].append((index, normalized))
        return list(groups.values())

    @staticmethod
    def _restore_order(
        size: int,
        grouped_results: list[tuple[list[tuple[int, SourceContent]], list[LoaderContent]]],
    ) -> list[LoaderContent]:
        ordered: list[LoaderContent | None] = [None] * size
        for indexed_sources, results in grouped_results:
            if len(indexed_sources) != len(results):
                raise RuntimeError("A routed loader returned an unexpected batch size")
            for (index, _), result in zip(indexed_sources, results, strict=True):
                ordered[index] = result
        if any(result is None for result in ordered):
            raise RuntimeError("AutoLoader failed to restore all batch results")
        return cast(list[LoaderContent], ordered)

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        **kwargs,
    ) -> list[LoaderContent]:
        """Route a batch by loader and preserve input order.

        Groups run sequentially so ``max_concurrency`` remains a global upper bound;
        each concrete loader still controls how its own group uses that budget.
        """

        self._validate_max_concurrency(max_concurrency)
        grouped_results = []
        for loader, indexed_sources in self._group_sources(sources):
            group_sources = [source for _, source in indexed_sources]
            results = loader.batch_load(
                group_sources, max_concurrency=max_concurrency, **kwargs
            )
            grouped_results.append((indexed_sources, results))
        return self._restore_order(len(sources), grouped_results)

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        **kwargs,
    ) -> list[LoaderContent]:
        self._validate_max_concurrency(max_concurrency)
        groups = await asyncio.to_thread(self._group_sources, sources)
        grouped_results = []
        for loader, indexed_sources in groups:
            group_sources = [source for _, source in indexed_sources]
            results = await loader.abatch_load(
                group_sources, max_concurrency=max_concurrency, **kwargs
            )
            grouped_results.append((indexed_sources, results))
        return self._restore_order(len(sources), grouped_results)

    def _unique_loaders(self) -> list[BaseLoader]:
        unique: dict[int, BaseLoader] = {}
        for route in self._routes:
            unique.setdefault(id(route.loader), route.loader)
        return list(unique.values())

    def cleanup(self) -> None:
        for loader in self._unique_loaders():
            loader.cleanup()

    async def acleanup(self) -> None:
        for loader in self._unique_loaders():
            await loader.acleanup()

    async def aclose(self) -> None:
        """Backward-compatible alias for ``acleanup``."""

        await self.acleanup()
