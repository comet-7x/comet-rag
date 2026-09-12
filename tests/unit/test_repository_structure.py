"""目录迁移的兼容性与规范路径测试。"""

from __future__ import annotations

import pytest


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


def test_source_loader_old_paths_preserve_object_identity() -> None:
    from comet_rag.engines.loaders import (
        AutoLoader as LegacyAutoLoader,
    )
    from comet_rag.engines.loaders import (
        LocalLoader as LegacyLocalLoader,
    )
    from comet_rag.engines.loaders import (
        URLLoader as LegacyURLLoader,
    )
    from comet_rag.infrastructure.sources import AutoLoader, LocalLoader, URLLoader

    assert LegacyAutoLoader is AutoLoader
    assert LegacyLocalLoader is LocalLoader
    assert LegacyURLLoader is URLLoader


def test_s3_loader_old_path_preserves_object_identity() -> None:
    from comet_rag.infrastructure.loaders import S3Loader as LegacyS3Loader
    from comet_rag.infrastructure.sources.s3 import S3Loader

    assert LegacyS3Loader is S3Loader


def test_loader_facade_exports_canonical_implementations() -> None:
    from comet_rag.infrastructure.sources import AutoLoader, LocalLoader, URLLoader
    from comet_rag.infrastructure.sources.s3 import S3Loader
    from comet_rag.loaders import (
        AutoLoader as PublicAutoLoader,
    )
    from comet_rag.loaders import (
        LocalLoader as PublicLocalLoader,
    )
    from comet_rag.loaders import (
        S3Loader as PublicS3Loader,
    )
    from comet_rag.loaders import (
        URLLoader as PublicURLLoader,
    )

    assert PublicAutoLoader is AutoLoader
    assert PublicLocalLoader is LocalLoader
    assert PublicURLLoader is URLLoader
    assert PublicS3Loader is S3Loader


def test_api_schema_old_path_preserves_object_identity() -> None:
    from comet_rag.api.schemas import SearchRequest
    from comet_rag.schemas import SearchRequest as LegacySearchRequest

    assert LegacySearchRequest is SearchRequest


def test_arq_executor_old_path_preserves_object_identity() -> None:
    pytest.importorskip("arq")

    from comet_rag.infrastructure.task_execution.arq import ArqExecutor
    from comet_rag.tasks.executor_arq import ArqExecutor as LegacyArqExecutor

    assert LegacyArqExecutor is ArqExecutor


def test_docx_old_paths_preserve_object_identity() -> None:
    from comet_rag.engines.cleaners.docx_cleaner import DocxCleaner as LegacyCleaner
    from comet_rag.engines.converters.text_converter import (
        DocxConverter as LegacyConverter,
    )
    from comet_rag.engines.document.docx import (
        DocxDocumentExtractor as LegacyExtractor,
    )
    from comet_rag.engines.documents.docx import (
        DocxCleaner,
        DocxConverter,
        DocxDocumentExtractor,
        DocxParser,
    )
    from comet_rag.engines.parsers.docx_parser.docx_parser import (
        DocxParser as LegacyParser,
    )

    assert LegacyCleaner is DocxCleaner
    assert LegacyConverter is DocxConverter
    assert LegacyExtractor is DocxDocumentExtractor
    assert LegacyParser is DocxParser
