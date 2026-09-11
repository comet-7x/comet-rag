"""来源加载的统一公共入口。

实现仍按依赖归属分布在 engines 与 infrastructure；这个门面只解决使用者发现性。
S3 名称通过模块级 ``__getattr__`` 惰性加载，核心 Loader 的导入不触碰可选 SDK。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from comet_rag.engines.loaders import (
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
from comet_rag.ports import SourceLoaderPort

if TYPE_CHECKING:
    from comet_rag.infrastructure.loaders import (
        MinioLoader,
        ObjectTooLarge,
        S3Loader,
    )

_S3_EXPORTS = frozenset({"MinioLoader", "ObjectTooLarge", "S3Loader"})


def __getattr__(name: str) -> Any:
    if name not in _S3_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from comet_rag.infrastructure import loaders as s3_loaders

    value = getattr(s3_loaders, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_S3_EXPORTS})


__all__ = [
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
