"""向量存储契约及其跨层值对象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class VectorStoreError(RuntimeError):
    """向量存储相关错误的基类。"""


class CollectionNotFound(VectorStoreError):
    def __init__(self, kb_id: str) -> None:
        super().__init__(f"知识库 {kb_id!r} 尚未创建，请先调用 aensure_collection()")
        self.kb_id = kb_id


class DimensionMismatch(VectorStoreError):
    """写入向量的维度与知识库声明的不符。

    必须报错而不能静默写入：更换 embedding 模型后，新旧向量混在同一空间
    会让检索质量无声劣化，而且无法判断哪些 chunk 需要重算。
    """

    def __init__(self, kb_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"知识库 {kb_id!r} 声明维度 {expected}，收到 {actual}。"
            f"通常意味着换了 embedding 模型 —— 请新建知识库或整库重算。"
        )
        self.kb_id, self.expected, self.actual = kb_id, expected, actual


class CollectionSchemaMismatch(VectorStoreError):
    """已有 collection 无法满足当前存储契约，需要显式重建。"""

    def __init__(self, kb_id: str, reasons: Sequence[str]) -> None:
        self.kb_id = kb_id
        self.reasons = tuple(reasons)
        detail = "；".join(self.reasons) or "schema 不兼容"
        super().__init__(
            f"知识库 {kb_id!r} 的 collection 不兼容当前 schema：{detail}。"
            "请先导出或确认原数据可重建，再显式删除 collection 并重新入库；"
            "系统不会自动删除现有数据。"
        )


@dataclass(slots=True)
class VectorRecord:
    """一条待写入的向量。

    ID 由调用方提供，使重复入库同一文档成为覆盖，而不是追加副本。
    """

    id: str
    text: str
    embedding: Sequence[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchHit:
    id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


# 过滤语义必须能被所有后端一致实现，因此暂时只开放等值、集合包含和 AND。
Filter = dict[str, Any]


def matches_filter(metadata: dict[str, Any], filter: Filter | None) -> bool:
    """返回元数据是否满足后端无关的过滤语义。"""
    if not filter:
        return True
    for key, expected in filter.items():
        actual = metadata.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


@runtime_checkable
class VectorSearchPort(Protocol):
    """只读向量检索能力，避免查询服务依赖完整存储生命周期。"""

    async def asearch(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]: ...


class BaseVectorStore(ABC):
    """完整向量存储契约；实现必须通过统一契约测试。"""

    @abstractmethod
    async def aensure_collection(self, kb_id: str, *, dim: int) -> None:
        """幂等准备知识库；已存在但维度不符时拒绝操作。"""

    @abstractmethod
    async def aupsert(self, kb_id: str, records: Sequence[VectorRecord]) -> list[str]:
        """按 ID 写入或覆盖，并在返回时保证数据可查。"""

    @abstractmethod
    async def asearch(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        """在指定知识库内按余弦相似度检索。"""

    @abstractmethod
    async def adelete(
        self,
        kb_id: str,
        *,
        ids: Sequence[str] | None = None,
        filter: Filter | None = None,
    ) -> int:
        """删除并返回删除条数；ID 与过滤条件至少提供一个。"""

    @abstractmethod
    async def adrop_collection(self, kb_id: str) -> None:
        """幂等删除整个知识库。"""

    @abstractmethod
    async def acount(self, kb_id: str, *, filter: Filter | None = None) -> int:
        """统计记录数，用于入库校验与配额控制。"""

    async def aclose(self) -> None:
        """释放连接；无资源的实现沿用空操作。"""
        return None


__all__ = [
    "BaseVectorStore",
    "CollectionNotFound",
    "CollectionSchemaMismatch",
    "DimensionMismatch",
    "Filter",
    "SearchHit",
    "VectorRecord",
    "VectorSearchPort",
    "VectorStoreError",
    "matches_filter",
]
