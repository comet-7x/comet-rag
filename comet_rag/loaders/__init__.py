from __future__ import annotations

# 来源加载的稳定公共入口。具体实现统一位于 infrastructure.sources；S3 保持
# 惰性加载，导入本门面不会触发可选 SDK。
from typing import TYPE_CHECKING, Any

from comet_rag.infrastructure.sources import (
    AsyncAutoLoader,
    AsyncBaseLoader,
    AsyncLoaderRoute,
    AsyncLocalLoader,
    AsyncURLLoader,
    AutoLoader,
    BaseLoader,
    DownloadRequestConfig,
    LoadedResource,
    LoaderContent,
    LoaderRoute,
    LocalLoader,
    SourceContent,
    URLLoader,
)
from comet_rag.ports.source import AsyncSourceLoaderPort, SourceLoaderPort

if TYPE_CHECKING:
    from comet_rag.infrastructure.sources.s3 import (
        AsyncMinioLoader,
        AsyncS3Loader,
        MinioLoader,
        ObjectTooLarge,
        S3Loader,
    )

_S3_EXPORTS = frozenset(
    {"AsyncMinioLoader", "AsyncS3Loader", "MinioLoader", "ObjectTooLarge", "S3Loader"}
)


def __getattr__(name: str) -> Any:
    if name not in _S3_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from comet_rag.infrastructure.sources import s3 as s3_loaders

    value = getattr(s3_loaders, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_S3_EXPORTS})


__all__ = [
    "AsyncAutoLoader",
    "AsyncBaseLoader",
    "AsyncLoaderRoute",
    "AsyncLocalLoader",
    "AsyncURLLoader",
    "AsyncSourceLoaderPort",
    "AsyncS3Loader",
    "AsyncMinioLoader",
    "AutoLoader",
    "BaseLoader",
    "DownloadRequestConfig",
    "LoadedResource",
    "LoaderContent",
    "LoaderRoute",
    "LocalLoader",
    "MinioLoader",
    "ObjectTooLarge",
    "S3Loader",
    "SourceContent",
    "SourceLoaderPort",
    "URLLoader",
]
