"""旧内存向量存储导入路径。"""

from __future__ import annotations

from comet_rag.infrastructure.persistence.vector_store.memory import (
    InMemoryVectorStore,
    cosine_similarity,
)

__all__ = ["InMemoryVectorStore", "cosine_similarity"]
