"""搜索相关 Pydantic 模型"""

from pydantic import BaseModel


class SearchRequest(BaseModel):
    """搜索请求"""

    query: str
    top_k: int = 5


class SearchResponse(BaseModel):
    """搜索响应"""

    results: list[dict]
    trace_id: str | None = None
