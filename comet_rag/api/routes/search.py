"""搜索相关路由"""

from fastapi import APIRouter

router = APIRouter(prefix="/search", tags=["search"])


@router.post("/")
async def search(query: str):
    """搜索接口"""
    return {"query": query}
