"""向量存储。

`BaseVectorStore` 是抽象，`InMemoryVectorStore` 是随包提供的实现；
Milvus 实现在 `milvus` extra 里（`pip install comet-rag[milvus]`），
故此处不做顶层导入 —— 否则没装 pymilvus 的用户 import 本包就会崩。
"""

from comet_rag.infrastructure.vectorstore.base import (
    BaseVectorStore,
    CollectionNotFound,
    DimensionMismatch,
    Filter,
    SearchHit,
    VectorRecord,
    VectorStoreError,
    matches_filter,
)
from comet_rag.infrastructure.vectorstore.memory import (
    InMemoryVectorStore,
    cosine_similarity,
)

__all__ = [
    "BaseVectorStore",
    "CollectionNotFound",
    "DimensionMismatch",
    "Filter",
    "InMemoryVectorStore",
    "SearchHit",
    "VectorRecord",
    "VectorStoreError",
    "cosine_similarity",
    "matches_filter",
]
