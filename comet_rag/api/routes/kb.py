"""知识库管理。

M1 阶段知识库 = 向量库里的一个集合；`knowledge_bases` 元数据表在 T19 落地，
届时 `embedding_dim` 等信息改为从表里读，而不是从配置推。
"""

from fastapi import APIRouter, status

from ...schemas.kb import KnowledgeBaseCreate, KnowledgeBaseInfo
from ..deps import ContextDep

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=KnowledgeBaseInfo)
async def create_kb(payload: KnowledgeBaseCreate, ctx: ContextDep) -> KnowledgeBaseInfo:
    """幂等：已存在且维度一致则原样返回，维度不一致会 409。"""
    await ctx.vector_store.aensure_collection(payload.kb_id, dim=ctx.embedding_dim)
    return KnowledgeBaseInfo(
        kb_id=payload.kb_id,
        chunk_count=await ctx.vector_store.acount(payload.kb_id),
        embedding_dim=ctx.embedding_dim,
    )


@router.get("/{kb_id}", response_model=KnowledgeBaseInfo)
async def get_kb(kb_id: str, ctx: ContextDep) -> KnowledgeBaseInfo:
    return KnowledgeBaseInfo(
        kb_id=kb_id,
        chunk_count=await ctx.vector_store.acount(kb_id),
        embedding_dim=ctx.embedding_dim,
    )


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(kb_id: str, ctx: ContextDep) -> None:
    """删除整个知识库。幂等：不存在也返回 204。"""
    await ctx.vector_store.adrop_collection(kb_id)
