from __future__ import annotations

from typing import TYPE_CHECKING

from .base import AsyncSourceResolver, SourceError, SourceResourceLimitExceeded
from .local import AsyncLocalSourceResolver
from .resolver import AsyncSourceRouter
from .s3 import AsyncS3SourceResolver
from .types import HttpSource, LocalResource, LocalSource, S3Source, Source

if TYPE_CHECKING:
    from .http import AsyncHttpSourceResolver


def __getattr__(name: str) -> object:
    if name == "AsyncHttpSourceResolver":
        from .http import AsyncHttpSourceResolver

        return AsyncHttpSourceResolver
    raise AttributeError(name)


__all__ = [
    "AsyncHttpSourceResolver",
    "AsyncLocalSourceResolver",
    "AsyncS3SourceResolver",
    "AsyncSourceResolver",
    "AsyncSourceRouter",
    "HttpSource",
    "LocalResource",
    "LocalSource",
    "S3Source",
    "Source",
    "SourceError",
    "SourceResourceLimitExceeded",
]
