"""Loader 值对象的兼容导入路径。"""

from comet_rag.ports.source import LoadedResource, SourceContent

# P1 先保留旧名称；两条导入路径必须指向同一个运行时类型。
LoaderContent = LoadedResource

__all__ = ["LoadedResource", "LoaderContent", "SourceContent"]
