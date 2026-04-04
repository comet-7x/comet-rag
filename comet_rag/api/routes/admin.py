"""管理相关路由"""

from fastapi import APIRouter

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}
