"""向量存储 Port 的分层与迁移兼容测试。"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

from comet_rag.infrastructure import vectorstore as legacy_package
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from comet_rag.infrastructure.vectorstore import base as legacy
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


def test_legacy_imports_keep_the_same_runtime_objects() -> None:
    """兼容层必须是别名；复制类会让旧路径捕获不到新路径抛出的异常。"""
    exports = {
        "BaseVectorStore": BaseVectorStore,
        "CollectionNotFound": CollectionNotFound,
        "CollectionSchemaMismatch": CollectionSchemaMismatch,
        "DimensionMismatch": DimensionMismatch,
        "Filter": Filter,
        "SearchHit": SearchHit,
        "VectorRecord": VectorRecord,
        "VectorSearchPort": VectorSearchPort,
        "VectorStoreError": VectorStoreError,
        "matches_filter": matches_filter,
    }
    for name, current in exports.items():
        assert getattr(legacy, name) is current
        assert getattr(legacy_package, name) is current


def test_ports_import_does_not_load_pymilvus() -> None:
    """基础安装应能使用契约，不应被可选的 Milvus SDK 拖累。"""
    code = "import sys; import comet_rag.ports; assert 'pymilvus' not in sys.modules"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_full_store_structurally_satisfies_narrow_search_port() -> None:
    assert isinstance(InMemoryVectorStore(), VectorSearchPort)


class _SearchOnly:
    async def asearch(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        return []


def test_search_port_does_not_require_write_or_lifecycle_methods() -> None:
    """查询用例不应因 Port 迁移而获得建库、删除或关闭能力。"""
    assert isinstance(_SearchOnly(), VectorSearchPort)
