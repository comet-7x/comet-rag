"""向量存储。

`BaseVectorStore` 是抽象，`InMemoryVectorStore` 是随包提供的实现；
Milvus 实现在 `milvus` extra 里（`pip install comet-rag[milvus]`），
故此处不做顶层导入 —— 否则没装 pymilvus 的用户 import 本包就会崩。
"""

from comet_rag.infrastructure.vectorstore.memory import (
    InMemoryVectorStore,
    cosine_similarity,
)
from comet_rag.ports import (
    BaseVectorStore,
    CollectionNotFound,
    CollectionSchemaMismatch,
    DimensionMismatch,
    Filter,
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
    "InMemoryVectorStore",
    "SearchHit",
    "VectorRecord",
    "VectorSearchPort",
    "VectorStoreError",
    "cosine_similarity",
    "matches_filter",
]
