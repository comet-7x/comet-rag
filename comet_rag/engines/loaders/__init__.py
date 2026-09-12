from __future__ import annotations

from importlib import import_module

# Loader 实现已迁出 engines。动态赋值保留旧包入口，也避免兼容层在 AST
# 依赖图中伪装成新的 engine → infrastructure 生产依赖。
_formats = import_module("comet_rag.engines.documents.formats")
_sources = import_module("comet_rag.infrastructure.sources")

AllowExt = _formats.AllowExt
BaseFileFormat = _formats.BaseFileFormat
CodeFormat = _formats.CodeFormat
ContentStructure = _formats.ContentStructure
ContentTypeMismatch = _formats.ContentTypeMismatch
FileFormat = _formats.FileFormat
FormatMeta = _formats.FormatMeta
GranularityStrategy = _formats.GranularityStrategy
MixedFormat = _formats.MixedFormat
ParseConfig = _formats.ParseConfig
ProseFormat = _formats.ProseFormat
SlideFormat = _formats.SlideFormat
StructuredFormat = _formats.StructuredFormat
TabularFormat = _formats.TabularFormat
UnsupportedContentType = _formats.UnsupportedContentType
is_allowed_extension = _formats.is_allowed_extension
normalize_extension = _formats.normalize_extension
resolve_detected_extension = _formats.resolve_detected_extension

AutoLoader = _sources.AutoLoader
BaseLoader = _sources.BaseLoader
DEFAULT_MAX_CONCURRENCY = _sources.DEFAULT_MAX_CONCURRENCY
DownloadRequestConfig = _sources.DownloadRequestConfig
LoadedResource = _sources.LoadedResource
LoaderContent = _sources.LoaderContent
LoaderRoute = _sources.LoaderRoute
LocalLoader = _sources.LocalLoader
SourceContent = _sources.SourceContent
URLLoader = _sources.URLLoader
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
