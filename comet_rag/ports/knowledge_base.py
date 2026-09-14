"""知识库领域对象与持久化契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


def _now() -> datetime:
    # 保持历史上的 CST 默认值，同时让 ports 继续只依赖标准库。
    return datetime.now(ZoneInfo("Asia/Shanghai"))


class KnowledgeBaseError(RuntimeError):
    """知识库相关错误的基类。"""


class KnowledgeBaseNotFound(KnowledgeBaseError):
    def __init__(self, kb_id: str) -> None:
        super().__init__(f"知识库不存在：{kb_id!r}")
        self.kb_id = kb_id


class KnowledgeBaseExists(KnowledgeBaseError):
    def __init__(self, kb_id: str) -> None:
        super().__init__(f"知识库已存在：{kb_id!r}")
        self.kb_id = kb_id


class EmbeddingModelChanged(KnowledgeBaseError):
    """拒绝把不同模型生成的向量写入同一个知识库。"""

    def __init__(self, kb_id: str, expected: str, actual: str) -> None:
        super().__init__(
            f"知识库 {kb_id!r} 建库时用的是 {expected!r}，当前配置为 {actual!r}。"
            f"混用会让新旧向量落在不同语义空间、检索静默劣化。"
            f"请改回原模型，或新建知识库并重新入库。"
        )
        self.kb_id, self.expected, self.actual = kb_id, expected, actual


@dataclass(slots=True)
class KnowledgeBase:
    kb_id: str
    name: str
    embedding_model: str
    embedding_dim: int
    description: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def assert_model_matches(self, model: str) -> None:
        if self.embedding_model != model:
            raise EmbeddingModelChanged(self.kb_id, self.embedding_model, model)


class KnowledgeBaseRepository(ABC):
    """知识库元数据存储；实现必须通过统一契约测试。"""

    @abstractmethod
    async def acreate(self, kb: KnowledgeBase) -> KnowledgeBase:
        """新建知识库，同 ID 已存在时拒绝。"""

    @abstractmethod
    async def aget(self, kb_id: str) -> KnowledgeBase | None: ...

    @abstractmethod
    async def alist(self, *, limit: int = 50, offset: int = 0) -> list[KnowledgeBase]:
        """按创建时间倒序返回知识库。"""

    @abstractmethod
    async def adelete(self, kb_id: str) -> bool:
        """删除知识库并返回是否实际删除。"""

    async def arequire(self, kb_id: str) -> KnowledgeBase:
        kb = await self.aget(kb_id)
        if kb is None:
            raise KnowledgeBaseNotFound(kb_id)
        return kb


__all__ = [
    "EmbeddingModelChanged",
    "KnowledgeBase",
    "KnowledgeBaseError",
    "KnowledgeBaseExists",
    "KnowledgeBaseNotFound",
    "KnowledgeBaseRepository",
]
