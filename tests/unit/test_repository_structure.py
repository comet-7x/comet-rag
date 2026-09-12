"""规范目录与稳定公共入口测试。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REMOVED_LEGACY_PATHS = (
    "comet_rag/engines/loaders",
    "comet_rag/engines/document",
    "comet_rag/engines/cleaners/docx_cleaner.py",
    "comet_rag/engines/parsers/docx_parser",
    "comet_rag/engines/pipelines/pipeline.py",
    "comet_rag/infrastructure/loaders",
    "comet_rag/infrastructure/providers",
    "comet_rag/infrastructure/database",
    "comet_rag/infrastructure/vectorstore",
    "comet_rag/infrastructure/knowledge_base.py",
    "comet_rag/schemas",
    "comet_rag/tasks/store_postgres.py",
    "comet_rag/tasks/executor_arq.py",
)


def test_legacy_paths_are_removed() -> None:
    """重构前的转发目录不得重新成为第二个实现位置。"""
    existing = []
    for path in REMOVED_LEGACY_PATHS:
        candidate = PROJECT_ROOT / path
        if candidate.is_file() or (
            candidate.is_dir() and any(candidate.rglob("*.py"))
        ):
            existing.append(path)

    assert existing == [], f"已废弃目录或模块重新出现：{existing}"


def test_loader_facade_exports_canonical_implementations() -> None:
    from comet_rag.infrastructure.sources import AutoLoader, LocalLoader, URLLoader
    from comet_rag.infrastructure.sources.s3 import S3Loader
    from comet_rag.loaders import AutoLoader as PublicAutoLoader
    from comet_rag.loaders import LocalLoader as PublicLocalLoader
    from comet_rag.loaders import S3Loader as PublicS3Loader
    from comet_rag.loaders import URLLoader as PublicURLLoader

    assert PublicAutoLoader is AutoLoader
    assert PublicLocalLoader is LocalLoader
    assert PublicURLLoader is URLLoader
    assert PublicS3Loader is S3Loader


def test_pipeline_facade_is_the_library_entry_point() -> None:
    from comet_rag.pipeline import Pipeline
    from comet_rag.services.pipeline import Pipeline as PipelineService

    assert issubclass(Pipeline, PipelineService)
