"""知识库接口出入参。"""

from pydantic import BaseModel, Field


class KnowledgeBaseCreate(BaseModel):
    kb_id: str = Field(..., min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None


class KnowledgeBaseInfo(BaseModel):
    kb_id: str
    name: str
    #: 建库时用的 embedding 模型与维度。换模型后往同一库灌数据会被拒绝——
    #: 新旧向量混在不同语义空间会让检索静默劣化（spec A12）。
    embedding_model: str
    embedding_dim: int
    #: 实时统计。-1 表示统计失败（例如 collection 已不存在），不是 0。
    chunk_count: int
    description: str | None = None
    created_at: str
    updated_at: str


class KnowledgeBaseList(BaseModel):
    knowledge_bases: list[KnowledgeBaseInfo]
    total: int
