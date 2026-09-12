"""KnowledgeBaseRepository 的进程内实现。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from comet_rag.ports.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseExists,
    KnowledgeBaseRepository,
)


class InMemoryKnowledgeBaseRepository(KnowledgeBaseRepository):
    """测试与零中间件开发使用；进程停止后数据丢失。"""

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
            return replace(row) if row else None

    async def alist(self, *, limit: int = 50, offset: int = 0) -> list[KnowledgeBase]:
        async with self._lock:
            rows = sorted(self._rows.values(), key=lambda k: k.created_at, reverse=True)
        return [replace(row) for row in rows[offset : offset + limit]]

    async def adelete(self, kb_id: str) -> bool:
        async with self._lock:
            return self._rows.pop(kb_id, None) is not None


__all__ = ["InMemoryKnowledgeBaseRepository"]
