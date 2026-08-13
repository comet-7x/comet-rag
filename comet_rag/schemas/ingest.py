"""入库接口出入参。"""

from typing import Any

from pydantic import BaseModel, Field


class IngestSubmit(BaseModel):
    kb_id: str = Field(..., min_length=1, description="目标知识库")
    source: str = Field(..., min_length=1, description="本地路径或 URL")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="附加到每个 chunk 的元数据"
    )
    idempotency_key: str | None = Field(
        default=None,
        description="防重复提交。同 key 重复提交返回既有任务，不会跑两遍。",
    )
    max_attempts: int = Field(default=3, ge=1, le=10, description="最大尝试次数")


class IngestAccepted(BaseModel):
    """入库是异步的，提交只返回受理凭据 —— 用 task_id 轮询进度。"""

    task_id: str
    status: str
    kb_id: str
