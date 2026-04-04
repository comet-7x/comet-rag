from datetime import datetime
from enum import Enum, StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_serializer, model_validator


class TaskStatus(StrEnum):
    """外部可见的粗粒度状态，用于 API 响应和监控大盘"""

    PENDING = "pending"
    PROCESSING = "processing"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStage(Enum):
    """
    内部细粒度阶段，每个值为 (step: int, parent_status: TaskStatus, queue: str)
    step         — 阶段顺序号，用于进度计算和定点重试路由
    parent_status — 对外暴露的粗粒度状态
    queue        — 该阶段由哪个 Celery 队列处理，重试时直接路由
    """

    WAITING_SCHEDULE = (0, TaskStatus.PENDING, "")
    CLASSIFYING = (1, TaskStatus.PROCESSING, "dispatcher")
    PREPROCESSING = (2, TaskStatus.PROCESSING, "preprocessor")
    PARSING = (3, TaskStatus.PROCESSING, "preprocessor")
    FINALIZING = (4, TaskStatus.PROCESSING, "preprocessor")
    CHUNKING = (5, TaskStatus.PROCESSING, "preprocessor")
    VECTORIZING = (6, TaskStatus.EMBEDDING, "embedder")
    UPLOADING = (7, TaskStatus.EMBEDDING, "embedder")
    DONE = (8, TaskStatus.COMPLETED, "")

    @property
    def step(self) -> int:
        return self.value[0]

    @property
    def parent_status(self) -> TaskStatus:
        return self.value[1]

    @property
    def queue(self) -> str:
        return self.value[2]

    @property
    def is_retryable(self) -> bool:
        """WAITING_SCHEDULE 和 DONE 不参与重试"""
        return self.queue != ""

    @classmethod
    def from_step(cls, step: int) -> "TaskStage":
        for member in cls:
            if member.step == step:
                return member
        raise ValueError(f"No TaskStage with step={step}")


class EmbeddingTaskResult(BaseModel):
    """任务完成后的结果元信息"""

    collection: str = Field(..., description="写入的向量数据库 collection 名称")
    partition: str = Field(..., description="写入的向量数据库 partition 名称")
    chunk_count: int = Field(..., description="生成的 chunk 数量")


class EmbeddingTaskInfo(BaseModel):
    task_id: str = Field(..., description="全局唯一任务 ID")
    file_url: str = Field(..., description="S3 文件地址")
    file_type: str | None = Field(
        default=None, description="文件类型，CLASSIFYING 阶段后填入"
    )

    status: TaskStatus = Field(default=TaskStatus.PENDING, description="粗粒度状态")
    stage: TaskStage = Field(
        default=TaskStage.WAITING_SCHEDULE, description="细粒度阶段"
    )

    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
    )

    error: str | None = Field(default=None, description="失败时的错误信息")
    retry_count: int = Field(default=0, description="已重试次数")
    result: EmbeddingTaskResult | None = Field(
        default=None, description="完成后的结果元信息"
    )

    # ── 序列化：stage 存为 step 整数，便于 MongoDB 存储和 API 返回 ──────────
    @field_serializer("stage")
    def serialize_stage(self, stage: TaskStage) -> int:
        return stage.step

    @field_serializer("status")
    def serialize_status(self, status: TaskStatus) -> str:
        return str(status)

    # ── 校验：status 必须与 stage.parent_status 一致 ─────────────────────────
    @model_validator(mode="after")
    def validate_status_stage_consistency(self) -> "EmbeddingTaskInfo":
        if self.status != TaskStatus.FAILED:
            expected = self.stage.parent_status
            if self.status != expected:
                raise ValueError(
                    f"status={self.status!r} 与 stage={self.stage.name} "
                    f"不匹配，期望 status={expected!r}"
                )
        return self

    # ── 状态流转方法 ──────────────────────────────────────────────────────────
    def advance_to(self, next_stage: TaskStage) -> None:
        """进入下一阶段，先更新状态再执行业务逻辑（由 Worker 在拉取任务后立即调用）"""
        self.stage = next_stage
        self.status = next_stage.parent_status
        self.updated_at = datetime.now(ZoneInfo("Asia/Shanghai"))
        self.error = None

    def mark_failed(self, error: str) -> None:
        """标记失败，保留当前 stage 作为重试锚点"""
        self.status = TaskStatus.FAILED
        self.error = error
        self.retry_count += 1
        self.updated_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    def mark_completed(self, result: EmbeddingTaskResult) -> None:
        self.stage = TaskStage.DONE
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.updated_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    @property
    def retry_stage(self) -> TaskStage:
        """重试时应该投递到的阶段（仅 FAILED 状态有意义）"""
        if self.status != TaskStatus.FAILED:
            raise RuntimeError("只有 FAILED 状态的任务才能重试")
        if not self.stage.is_retryable:
            raise RuntimeError(f"stage={self.stage.name} 不支持重试")
        return self.stage

    @property
    def progress(self) -> str:
        total = TaskStage.DONE.step
        return f"{self.stage.step}/{total}"
