"""向量存储 Port 的分层测试。"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import (
    AsyncKeywordSearchPort,
    AsyncVectorSearchPort,
    AsyncVectorStore,
    Filter,
    SearchHit,
)


def test_concrete_store_satisfies_vector_store_port() -> None:
    assert isinstance(AsyncInMemoryVectorStore(), AsyncVectorStore)


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
    store = AsyncInMemoryVectorStore()
    assert isinstance(store, AsyncVectorSearchPort)
    assert isinstance(store, AsyncKeywordSearchPort)


class _SearchOnly:
    async def search(
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
    assert isinstance(_SearchOnly(), AsyncVectorSearchPort)
