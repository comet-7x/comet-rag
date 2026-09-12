"""知识库旧导入路径；新代码应从 ports 或 persistence 导入。"""

from __future__ import annotations

from comet_rag.infrastructure.persistence.knowledge_base.memory import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.ports.knowledge_base import (
    EmbeddingModelChanged,
    KnowledgeBase,
    KnowledgeBaseError,
    KnowledgeBaseExists,
    KnowledgeBaseNotFound,
    KnowledgeBaseRepository,
)

__all__ = [
    "EmbeddingModelChanged",
    "InMemoryKnowledgeBaseRepository",
    "KnowledgeBase",
    "KnowledgeBaseError",
    "KnowledgeBaseExists",
    "KnowledgeBaseNotFound",
    "KnowledgeBaseRepository",
]
