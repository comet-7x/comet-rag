"""旧向量存储导入路径的兼容层。

新代码应从 :mod:`comet_rag.ports` 导入契约；这里保留对象别名，避免已有使用者
迁移期间出现类身份不一致或异常捕获失效。
"""

from __future__ import annotations

from comet_rag.ports.vector_store import (
    BaseVectorStore,
    CollectionNotFound,
    CollectionSchemaMismatch,
    DimensionMismatch,
    Filter,
    KeywordSearchPort,
    SearchHit,
    VectorRecord,
    VectorSearchPort,
    VectorStoreError,
    matches_filter,
)

__all__ = [
    "BaseVectorStore",
    "CollectionNotFound",
    "CollectionSchemaMismatch",
    "DimensionMismatch",
    "Filter",
    "KeywordSearchPort",
    "SearchHit",
    "VectorRecord",
    "VectorSearchPort",
    "VectorStoreError",
    "matches_filter",
]
