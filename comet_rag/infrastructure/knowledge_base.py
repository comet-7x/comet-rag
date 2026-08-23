"""知识库元数据。

## 为什么知识库必须是一张表，而不是一个字符串标签

**`embedding_model` 与 `embedding_dim` 必须被记录下来**（spec A12）。

不记的话，哪天换了 embedding 模型继续往同一个库里灌，新旧向量就混在同一个
向量空间里 —— 不报错、不崩溃，只是检索质量悄悄下滑；而且事后**无法分辨
哪些 chunk 是旧模型算的、该重算哪些**，只能整库重灌。

维度不一致还能被向量库拦住（`DimensionMismatch`），但**同维度的不同模型**
谁也拦不住 —— 只有这张表能。

## 为什么又是 ABC

Phase 3 的端到端链路必须能在零中间件下跑通（plan Checkpoint C/D）。
知识库元数据若只有 Postgres 一种实现，那条性质当场就没了。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime

from comet_rag.core.time import Time


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
    """建库时用的 embedding 模型与当前配置的不一致。

    **必须拒绝，不能将就**：同维度的两个不同模型产出的向量落在完全不同的
    语义空间里，混在一起检索会静默劣化 —— 没有任何报错，只是结果变差，
    而且事后分不清哪些 chunk 该重算。
    """

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
    created_at: datetime = field(default_factory=Time.now)
    updated_at: datetime = field(default_factory=Time.now)

    def assert_model_matches(self, model: str) -> None:
        if self.embedding_model != model:
            raise EmbeddingModelChanged(self.kb_id, self.embedding_model, model)


class KnowledgeBaseRepository(ABC):
    """知识库元数据存储。实现必须通过 `tests/contracts/knowledge_base.py`。"""

    @abstractmethod
    async def acreate(self, kb: KnowledgeBase) -> KnowledgeBase:
        """新建。同 id 已存在则抛 `KnowledgeBaseExists`。"""

    @abstractmethod
    async def aget(self, kb_id: str) -> KnowledgeBase | None: ...

    @abstractmethod
    async def alist(self, *, limit: int = 50, offset: int = 0) -> list[KnowledgeBase]:
        """按创建时间倒序。"""

    @abstractmethod
    async def adelete(self, kb_id: str) -> bool:
        """删除并返回是否真的删掉了（不存在返回 False，不报错）。"""

    async def arequire(self, kb_id: str) -> KnowledgeBase:
        kb = await self.aget(kb_id)
        if kb is None:
            raise KnowledgeBaseNotFound(kb_id)
        return kb


class InMemoryKnowledgeBaseRepository(KnowledgeBaseRepository):
    """进程内实现。测试与零中间件开发用，进程一停即丢。"""

    def __init__(self) -> None:
        self._rows: dict[str, KnowledgeBase] = {}
        self._lock = asyncio.Lock()

    async def acreate(self, kb: KnowledgeBase) -> KnowledgeBase:
        async with self._lock:
            if kb.kb_id in self._rows:
                raise KnowledgeBaseExists(kb.kb_id)
            self._rows[kb.kb_id] = replace(kb)
            return replace(kb)

    async def aget(self, kb_id: str) -> KnowledgeBase | None:
        async with self._lock:
            row = self._rows.get(kb_id)
            # 返回副本：返回活对象会让调用方绕过仓储直接改"库"
            return replace(row) if row else None

    async def alist(self, *, limit: int = 50, offset: int = 0) -> list[KnowledgeBase]:
        async with self._lock:
            rows = sorted(self._rows.values(), key=lambda k: k.created_at, reverse=True)
        return [replace(r) for r in rows[offset : offset + limit]]

    async def adelete(self, kb_id: str) -> bool:
        async with self._lock:
            return self._rows.pop(kb_id, None) is not None


__all__ = [
    "EmbeddingModelChanged",
    "InMemoryKnowledgeBaseRepository",
    "KnowledgeBase",
    "KnowledgeBaseError",
    "KnowledgeBaseExists",
    "KnowledgeBaseNotFound",
    "KnowledgeBaseRepository",
]
