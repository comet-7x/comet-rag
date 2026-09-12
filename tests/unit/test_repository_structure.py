"""目录迁移的兼容性与规范路径测试。"""

from __future__ import annotations


def test_knowledge_base_old_path_preserves_object_identity() -> None:
    from comet_rag.infrastructure.knowledge_base import (
        InMemoryKnowledgeBaseRepository as LegacyMemoryRepository,
    )
    from comet_rag.infrastructure.knowledge_base import KnowledgeBase as LegacyModel
    from comet_rag.infrastructure.persistence.knowledge_base.memory import (
        InMemoryKnowledgeBaseRepository,
    )
    from comet_rag.ports import KnowledgeBase

    assert LegacyModel is KnowledgeBase
    assert LegacyMemoryRepository is InMemoryKnowledgeBaseRepository


def test_vector_store_old_path_preserves_object_identity() -> None:
    from comet_rag.infrastructure.persistence.vector_store.memory import (
        InMemoryVectorStore,
    )
    from comet_rag.infrastructure.vectorstore import (
        InMemoryVectorStore as LegacyVectorStore,
    )

    assert LegacyVectorStore is InMemoryVectorStore


def test_sql_old_path_preserves_object_identity() -> None:
    from comet_rag.infrastructure.database import Database as LegacyDatabase
    from comet_rag.infrastructure.persistence.sql import Database

    assert LegacyDatabase is Database


def test_postgres_task_store_old_path_is_lazy_and_compatible() -> None:
    from comet_rag.infrastructure.persistence.task_store.postgres import (
        PostgresTaskStore,
    )
    from comet_rag.tasks.store_postgres import PostgresTaskStore as LegacyTaskStore

    assert LegacyTaskStore is PostgresTaskStore
