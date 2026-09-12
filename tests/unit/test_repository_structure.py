"""目录迁移的兼容性与规范路径测试。"""

from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LEGACY_IMPLEMENTATION_MODULES = (
    "comet_rag/engines/loaders/auto_loader.py",
    "comet_rag/engines/loaders/base_loader.py",
    "comet_rag/engines/loaders/data_type.py",
    "comet_rag/engines/loaders/file_info.py",
    "comet_rag/engines/loaders/local_loader.py",
    "comet_rag/engines/loaders/url_loader.py",
    "comet_rag/engines/cleaners/docx_cleaner.py",
    "comet_rag/engines/document/docx/extractor.py",
    "comet_rag/engines/parsers/docx_parser/docx_parser.py",
    "comet_rag/engines/parsers/docx_parser/latex_dict.py",
    "comet_rag/engines/parsers/docx_parser/omml.py",
    "comet_rag/infrastructure/loaders/s3_loader.py",
    "comet_rag/infrastructure/providers/base.py",
    "comet_rag/infrastructure/providers/_embedding_wire.py",
    "comet_rag/infrastructure/providers/_image_reference.py",
    "comet_rag/infrastructure/providers/document/mineru.py",
    "comet_rag/infrastructure/providers/embedding/base.py",
    "comet_rag/infrastructure/providers/embedding/openai_embedding_model.py",
    "comet_rag/infrastructure/providers/embedding/qwen3_vl_embedding.py",
    "comet_rag/infrastructure/providers/reranker/base.py",
    "comet_rag/infrastructure/providers/reranker/qwen3_vl_reranker.py",
    "comet_rag/infrastructure/providers/vision/openai_vision_model.py",
    "comet_rag/infrastructure/vectorstore/memory.py",
    "comet_rag/infrastructure/vectorstore/milvus.py",
    "comet_rag/tasks/executor_arq.py",
    "comet_rag/schemas/ingest.py",
    "comet_rag/schemas/kb.py",
    "comet_rag/schemas/search.py",
    "comet_rag/schemas/task.py",
)


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

    LegacyArqExecutor = import_module(
        "comet_rag.tasks.executor_arq"
    ).ArqExecutor

    assert LegacyArqExecutor is ArqExecutor


def test_docx_old_paths_preserve_object_identity() -> None:
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

    LegacyCleaner = import_module(
        "comet_rag.engines.cleaners.docx_cleaner"
    ).DocxCleaner
    LegacyParser = import_module(
        "comet_rag.engines.parsers.docx_parser.docx_parser"
    ).DocxParser

    assert LegacyCleaner is DocxCleaner
    assert LegacyConverter is DocxConverter
    assert LegacyExtractor is DocxDocumentExtractor
    assert LegacyParser is DocxParser


def test_pipeline_old_path_preserves_public_facade_identity() -> None:
    from comet_rag.engines.pipelines import Pipeline as LegacyPipeline
    from comet_rag.pipeline import Pipeline

    assert LegacyPipeline is Pipeline


def test_model_provider_old_paths_preserve_object_identity() -> None:
    from comet_rag.infrastructure.models.embedding import BaseEmbeddingModel
    from comet_rag.infrastructure.models.reranker import BaseReranker
    from comet_rag.infrastructure.providers.embedding import (
        BaseEmbeddingModel as LegacyEmbeddingBase,
    )
    from comet_rag.infrastructure.providers.reranker import (
        BaseReranker as LegacyRerankerBase,
    )

    assert LegacyEmbeddingBase is BaseEmbeddingModel
    assert LegacyRerankerBase is BaseReranker


def test_document_provider_old_path_preserves_object_identity() -> None:
    from comet_rag.infrastructure.extractors import MinerUDocumentExtractor
    from comet_rag.infrastructure.providers.document import (
        MinerUDocumentExtractor as LegacyMinerU,
    )

    assert LegacyMinerU is MinerUDocumentExtractor


@pytest.mark.parametrize("relative_path", LEGACY_IMPLEMENTATION_MODULES)
def test_legacy_implementation_module_defines_no_classes(relative_path: str) -> None:
    """旧路径只能转发；重新出现类定义意味着实现又分叉成两份。"""
    path = PROJECT_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]

    assert classes == [], f"兼容模块 {relative_path} 重新定义了实现：{classes}"
