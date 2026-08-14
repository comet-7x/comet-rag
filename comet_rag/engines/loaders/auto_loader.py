from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from comet_rag.engines.loaders.base_loader import (
    DEFAULT_MAX_CONCURRENCY,
    BaseLoader,
)
from comet_rag.engines.loaders.types import LoaderContent, SourceContent

LoaderMatcher = Callable[[SourceContent], bool]


@dataclass(frozen=True, slots=True)
class LoaderRoute:
    """A named routing rule used by :class:`AutoLoader`.

    Matchers keep routing open to infrastructure adapters without teaching engines
    about their SDKs or extending a closed source-type enum.
    """

    name: str
    loader: BaseLoader
    matcher: LoaderMatcher

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("LoaderRoute.name must not be empty")

    @classmethod
    def local(cls, loader: BaseLoader, *, name: str = "local") -> LoaderRoute:
        return cls(name=name, loader=loader, matcher=lambda source: source.is_local)

    @classmethod
    def schemes(
        cls,
        name: str,
        loader: BaseLoader,
        schemes: Iterable[str],
    ) -> LoaderRoute:
        normalized = frozenset(scheme.strip().lower() for scheme in schemes)
        if not normalized or "" in normalized:
            raise ValueError("LoaderRoute.schemes requires non-empty schemes")
        return cls(
            name=name,
            loader=loader,
            matcher=lambda source: (
                source.parsed_url.scheme.lower() in normalized
                and bool(source.parsed_url.netloc)
            ),
        )


class AutoLoader(BaseLoader):
    """Route sources through one explicit ``LoaderRoute`` collection."""

    def __init__(
        self,
        routes: Sequence[LoaderRoute],
    ) -> None:
        self._routes = list(routes)
        self._validate_route_names(self._routes)

    @staticmethod
    def default_routes(
        download_dir: str | Path | None = None,
        max_download_bytes: int | None = None,
        redirect_validator: Callable[[str], None] | None = None,
    ) -> list[LoaderRoute]:
        from comet_rag.engines.loaders.local_loader import LocalLoader  # noqa: PLC0415
        from comet_rag.engines.loaders.url_loader import (  # noqa: PLC0415
            DEFAULT_MAX_DOWNLOAD_BYTES,
            URLLoader,
        )

        return [
            LoaderRoute.local(LocalLoader()),
            LoaderRoute.schemes(
                "url",
                URLLoader(
                    download_dir=download_dir,
                    max_download_bytes=(
                        max_download_bytes
                        if max_download_bytes is not None
                        else DEFAULT_MAX_DOWNLOAD_BYTES
                    ),
                    redirect_validator=redirect_validator,
                ),
                {"http", "https"},
            ),
        ]

    @classmethod
    def default(
        cls,
        download_dir: str | Path | None = None,
        max_download_bytes: int | None = None,
        redirect_validator: Callable[[str], None] | None = None,
    ) -> AutoLoader:
        return cls(
            cls.default_routes(
                download_dir=download_dir,
                max_download_bytes=max_download_bytes,
                redirect_validator=redirect_validator,
            )
        )

    @staticmethod
    def _validate_route_names(routes: Sequence[LoaderRoute]) -> None:
        names = [route.name for route in routes]
        if len(names) != len(set(names)):
            raise ValueError("Loader route names must be unique")

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

    def load(self, source: SourceContent | str) -> LoaderContent:
        normalized = self._normalize_source(source)
        return self._resolve(normalized).load(normalized)

    async def aload(self, source: SourceContent | str) -> LoaderContent:
        normalized = self._normalize_source(source)
        # Route matching may inspect the local filesystem. Keep that blocking stat
        # off the event loop for local paths and custom network filesystems.
        loader = await asyncio.to_thread(self._resolve, normalized)
        return await loader.aload(normalized)

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
        grouped_results: list[
            tuple[list[tuple[int, SourceContent]], list[LoaderContent]]
        ],
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
    ) -> list[LoaderContent]:
        """Route a batch by loader and preserve input order.

        Groups run sequentially so ``max_concurrency`` remains a global upper bound;
        each concrete loader still controls how its own group uses that budget.
        """

        self._validate_max_concurrency(max_concurrency)
        grouped_results = []
        for loader, indexed_sources in self._group_sources(sources):
            group_sources = [source for _, source in indexed_sources]
            results = loader.batch_load(group_sources, max_concurrency=max_concurrency)
            grouped_results.append((indexed_sources, results))
        return self._restore_order(len(sources), grouped_results)

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[LoaderContent]:
        self._validate_max_concurrency(max_concurrency)
        groups = await asyncio.to_thread(self._group_sources, sources)
        grouped_results = []
        for loader, indexed_sources in groups:
            group_sources = [source for _, source in indexed_sources]
            results = await loader.abatch_load(
                group_sources, max_concurrency=max_concurrency
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
