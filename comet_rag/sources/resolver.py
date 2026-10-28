from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from .base import AsyncSourceResolver
from .local import AsyncLocalSourceResolver
from .types import HttpSource, LocalResource, LocalSource, S3Source, Source


class AsyncSourceRouter:
    def __init__(
        self,
        *,
        local: AsyncSourceResolver | None = None,
        http: AsyncSourceResolver | None = None,
        s3: AsyncSourceResolver | None = None,
    ) -> None:
        self._local = local if local is not None else AsyncLocalSourceResolver()
        self._http = http
        self._s3 = s3

    def resolve(self, source: Source) -> AbstractAsyncContextManager[LocalResource]:
        if isinstance(source, LocalSource):
            resolver = self._local
        elif isinstance(source, HttpSource):
            resolver = self._http
        elif isinstance(source, S3Source):
            resolver = self._s3
        else:
            raise TypeError("source router requires an explicit source type")
        if resolver is None:
            raise ValueError(f"no resolver configured for {type(source).__name__}")
        return resolver.resolve(source)
