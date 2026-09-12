from __future__ import annotations

# 来源加载适配器。
from comet_rag.engines.documents.formats import (
    AllowExt,
    BaseFileFormat,
    CodeFormat,
    ContentStructure,
    ContentTypeMismatch,
    FileFormat,
    FormatMeta,
    GranularityStrategy,
    MixedFormat,
    ParseConfig,
    ProseFormat,
    SlideFormat,
    StructuredFormat,
    TabularFormat,
    UnsupportedContentType,
    is_allowed_extension,
    normalize_extension,
    resolve_detected_extension,
)
from comet_rag.ports.source import LoadedResource, SourceContent

from .base import DEFAULT_MAX_CONCURRENCY, BaseLoader
from .http import DownloadRequestConfig, URLLoader
from .local import LocalLoader
from .router import AutoLoader, LoaderRoute

LoaderContent = LoadedResource
Loader = AutoLoader

__all__ = [
    "AllowExt",
    "AutoLoader",
    "BaseFileFormat",
    "BaseLoader",
    "CodeFormat",
    "ContentStructure",
    "ContentTypeMismatch",
    "DEFAULT_MAX_CONCURRENCY",
    "DownloadRequestConfig",
    "FileFormat",
    "FormatMeta",
    "GranularityStrategy",
    "LoadedResource",
    "Loader",
    "LoaderContent",
    "LoaderRoute",
    "LocalLoader",
    "MixedFormat",
    "ParseConfig",
    "ProseFormat",
    "SlideFormat",
    "SourceContent",
    "StructuredFormat",
    "TabularFormat",
    "URLLoader",
    "UnsupportedContentType",
    "is_allowed_extension",
    "normalize_extension",
    "resolve_detected_extension",
]
