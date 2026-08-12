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
    #: 实际生效的 top_k。服务降级到 L2 时会小于请求值。
    #: 没有它的话，客户端分不清"结果少是因为库里就这么多"还是"服务在降级"——
    #: 前者该改查询，后者该等一等或扩容，处理方式完全相反。
    effective_top_k: int = 0
    #: 当前降级级别；正常时为 null。
    degraded: str | None = None
