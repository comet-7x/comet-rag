"""知识库接口出入参。

M1 阶段知识库只是向量库里的一个集合；`knowledge_bases` 元数据表在 T19 落地。
"""

from pydantic import BaseModel, Field


class KnowledgeBaseCreate(BaseModel):
    kb_id: str = Field(..., min_length=1, max_length=128)


class KnowledgeBaseInfo(BaseModel):
    kb_id: str
    chunk_count: int
    #: 记录建库时的向量维度。换 embedding 模型后新旧向量混在同一空间会让
    #: 检索静默劣化，这个字段是事后能分辨"该重算哪些"的唯一线索（spec A12）。
    embedding_dim: int
