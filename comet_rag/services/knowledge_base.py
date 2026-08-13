"""知识库用例：元数据表与向量库的一致性维护。

这两者必须**成对操作**，否则会漂移成两种都很难查的状态：
  · 有元数据没 collection → 入库时莫名报 `CollectionNotFound`
  · 有 collection 没元数据 → 检索得到结果，但没人知道它是哪个模型算的

删除尤其要注意顺序：**先删向量、后删元数据**。反过来的话，中途失败会留下
一堆无主向量 —— 没有元数据就没人知道它们属于谁、该不该清，只能人工翻库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from comet_rag.core.logging import logger
from comet_rag.infrastructure.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseExists,
    KnowledgeBaseRepository,
)
from comet_rag.infrastructure.vectorstore import BaseVectorStore


class KnowledgeBaseSpec(BaseModel):
    """建库请求。"""

    kb_id: str = Field(..., min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None


@dataclass(slots=True)
class KnowledgeBaseView:
    """对外视图：元数据 + 实时统计。

    `chunk_count` 每次现查向量库而非存在表里 —— 冗余计数一定会和真实值
    漂移（写入失败、外部直接删数据），而漂移了的计数比没有计数更误导人。
    """

    kb_id: str
    name: str
    embedding_model: str
    embedding_dim: int
    chunk_count: int
    description: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kb_id": self.kb_id,
            "name": self.name,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "chunk_count": self.chunk_count,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class KnowledgeBaseService:
    def __init__(
        self,
        *,
        repository: KnowledgeBaseRepository,
        vector_store: BaseVectorStore,
        embedding_model: str,
        embedding_dim: int,
    ) -> None:
        self._repo = repository
        self._store = vector_store
        self._model = embedding_model
        self._dim = embedding_dim

    async def create(self, spec: KnowledgeBaseSpec) -> KnowledgeBaseView:
        """建库。**幂等**：已存在且模型一致则原样返回。

        幂等是必需的 —— 客户端常在每次入库前"确保库存在"，
        不幂等就得让每个调用方自己先查后建，那是把竞态推给调用方。
        """
        existing = await self._repo.aget(spec.kb_id)
        if existing is not None:
            # 模型变了要拒绝，不能悄悄沿用旧库（spec A12）
            existing.assert_model_matches(self._model)
            await self._store.aensure_collection(
                existing.kb_id, dim=existing.embedding_dim
            )
            return await self._view(existing)

        kb = KnowledgeBase(
            kb_id=spec.kb_id,
            name=spec.name or spec.kb_id,
            embedding_model=self._model,
            embedding_dim=self._dim,
            description=spec.description,
        )
        # 先建 collection 再写元数据：反过来的话，建表失败会留下一条
        # 指向不存在 collection 的元数据，之后每次入库都报 CollectionNotFound。
        await self._store.aensure_collection(kb.kb_id, dim=kb.embedding_dim)
        try:
            created = await self._repo.acreate(kb)
        except KnowledgeBaseExists:
            # 另一个并发请求赢得了主键竞争。读取赢家并执行与顺序重试完全
            # 相同的兼容性检查；同模型则幂等返回，不同模型仍按 A12 拒绝。
            created = await self._repo.arequire(kb.kb_id)
            created.assert_model_matches(self._model)
            await self._store.aensure_collection(
                created.kb_id, dim=created.embedding_dim
            )
            return await self._view(created)
        logger.info(
            f"知识库已创建 kb={created.kb_id} model={created.embedding_model} "
            f"dim={created.embedding_dim}"
        )
        return await self._view(created)

    async def get(self, kb_id: str) -> KnowledgeBaseView:
        return await self._view(await self._repo.arequire(kb_id))

    async def list(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[KnowledgeBaseView]:
        rows = await self._repo.alist(limit=limit, offset=offset)
        return [await self._view(row) for row in rows]

    async def delete(self, kb_id: str) -> bool:
        """删库。先删向量、后删元数据 —— 反过来会留下无主向量。"""
        kb = await self._repo.aget(kb_id)
        if kb is None:
            return False
        await self._store.adrop_collection(kb_id)
        deleted = await self._repo.adelete(kb_id)
        logger.info(f"知识库已删除 kb={kb_id}")
        return deleted

    async def resolve_for_ingest(self, kb_id: str) -> KnowledgeBase:
        """入库前的一致性检查：库必须存在，且模型必须与建库时一致。

        这是 A12 的执行点。维度不符会被向量库拦下，但**同维度的不同模型**
        谁也拦不住 —— 只有这里能。
        """
        kb = await self._repo.arequire(kb_id)
        kb.assert_model_matches(self._model)
        return kb

    async def resolve_for_search(self, kb_id: str) -> KnowledgeBase:
        """检索前执行与入库相同的模型一致性守卫。"""
        kb = await self._repo.arequire(kb_id)
        kb.assert_model_matches(self._model)
        return kb

    async def _view(self, kb: KnowledgeBase) -> KnowledgeBaseView:
        try:
            count = await self._store.acount(kb.kb_id)
        except Exception as exc:  # noqa: BLE001 —— 统计失败不该让查询整个失败
            logger.warning(f"统计 kb={kb.kb_id} 的 chunk 数失败：{exc!r}")
            count = -1
        return KnowledgeBaseView(
            kb_id=kb.kb_id,
            name=kb.name,
            embedding_model=kb.embedding_model,
            embedding_dim=kb.embedding_dim,
            chunk_count=count,
            description=kb.description,
            created_at=kb.created_at.isoformat(),
            updated_at=kb.updated_at.isoformat(),
        )


__all__ = ["KnowledgeBaseService", "KnowledgeBaseSpec", "KnowledgeBaseView"]
