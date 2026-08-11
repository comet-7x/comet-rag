"""检索接口出入参。"""

from typing import Any

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    kb_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, gt=0, le=100)
    fetch_k: int | None = Field(
        default=None, gt=0, le=500, description="送进重排的候选数，缺省为 top_k 的 4 倍"
    )
    filter: dict[str, Any] | None = Field(
        default=None, description="元数据过滤：{字段: 值} 或 {字段: [值1, 值2]}"
    )
    rerank: bool = Field(
        default=True, description="是否重排（未配置 reranker 时自动跳过）"
    )


class SearchResultItem(BaseModel):
    id: str
    text: str
    score: float
    metadata: dict[str, Any]
    vector_score: float | None = None


class SearchResponse(BaseModel):
    chunks: list[SearchResultItem]
    #: 重排是否真的执行了。false 可能意味着未配置、被显式关闭，**或已降级** ——
    #: 客户端据此判断结果质量。
    reranked: bool
    fetched: int
