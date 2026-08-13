"""知识库管理。"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from ...schemas.kb import KnowledgeBaseCreate, KnowledgeBaseInfo, KnowledgeBaseList
from ...services.knowledge_base import KnowledgeBaseSpec
from ..deps import KnowledgeBaseDep

router = APIRouter(prefix="/kb", tags=["knowledge-base"])

LimitQuery = Annotated[int, Query(gt=0, le=200)]
OffsetQuery = Annotated[int, Query(ge=0)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=KnowledgeBaseInfo)
async def create_kb(
    payload: KnowledgeBaseCreate, service: KnowledgeBaseDep
) -> KnowledgeBaseInfo:
    """建库。**幂等**：已存在且 embedding 模型一致则原样返回；
    模型变了返回 409（混用会让检索静默劣化，见 spec A12）。"""
    view = await service.create(
        KnowledgeBaseSpec(
            kb_id=payload.kb_id, name=payload.name, description=payload.description
        )
    )
    return KnowledgeBaseInfo(**view.to_dict())


@router.get("", response_model=KnowledgeBaseList)
async def list_kb(
    service: KnowledgeBaseDep, limit: LimitQuery = 50, offset: OffsetQuery = 0
) -> KnowledgeBaseList:
    views = await service.list(limit=limit, offset=offset)
    return KnowledgeBaseList(
        knowledge_bases=[KnowledgeBaseInfo(**v.to_dict()) for v in views],
        total=len(views),
    )


@router.get("/{kb_id}", response_model=KnowledgeBaseInfo)
async def get_kb(kb_id: str, service: KnowledgeBaseDep) -> KnowledgeBaseInfo:
    return KnowledgeBaseInfo(**(await service.get(kb_id)).to_dict())


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(kb_id: str, service: KnowledgeBaseDep) -> None:
    """删库：先删向量、后删元数据。幂等，不存在也返回 204。"""
    await service.delete(kb_id)
