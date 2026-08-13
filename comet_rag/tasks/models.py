"""通用任务记录与状态（产品无关）。

任何领域的长任务都可复用：`kind` 区分业务种类，`context` 给多阶段 runner 存中间态，
`result` 放最终产物，`resume_stage` 承载断点续跑。

设计约束（贯穿全包）：
1. **Task 必须可序列化**。`request / context / result` 只允许放
   JSON 友好的值；大产物（PPT、图片、音频）只存 `result_uri` 引用，不要塞二进制。
   这是「进程重启后能续跑」的前提。
2. **status 只能经状态机迁移**（见 states.py），任何地方都不允许裸 `task.status = X`。
3. **version 是乐观锁**，每次写 +1；并发写靠 CAS 失败重试，而不是靠祈祷。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timezone
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo


class TimeZone(StrEnum):
    """常用时区枚举（可随时扩展常用别名）"""

    CST = "Asia/Shanghai"  # 中国标准时间 (UTC+8)
    UTC = "UTC"  # 协调世界时
    EST = "America/New_York"  # 美东时间
    PST = "America/Los_Angeles"  # 美西时间
    JST = "Asia/Tokyo"  # 日本标准时间 (UTC+9)
    GMT = "Europe/London"  # 格林威治标准时间


type TZType = TimeZone | str | ZoneInfo | timezone


class Time:
    """
    时间工厂工具类：统一返回带时区信息的 Python 标准 `datetime` 对象。
    不再封装自定义包装层，避免重载运算符带来的类型推导麻烦。
    """

    @staticmethod
    def _parse_tz(tz: TZType) -> ZoneInfo | timezone:
        if isinstance(tz, (ZoneInfo, timezone)):
            return tz
        return ZoneInfo(str(tz))

    @classmethod
    def now(cls, tz: TZType = TimeZone.CST) -> datetime:
        """获取指定时区的当前 datetime（默认 CST）"""
        return datetime.now(cls._parse_tz(tz))

    @classmethod
    def utcnow(cls) -> datetime:
        """获取当前 UTC 时区的 datetime"""
        return datetime.now(UTC)

    @classmethod
    def to(cls, dt: datetime, tz: TZType) -> datetime:
        """将已有 datetime 转换到指定目标时区"""
        tz_obj = cls._parse_tz(tz)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz_obj)
        return dt.astimezone(tz_obj)

    @classmethod
    def iso(cls, dt: datetime | None = None, tz: TZType = TimeZone.CST) -> str:
        """直接获取或转换 ISO 8601 格式字符串"""
        target_dt = dt or cls.now(tz)
        return target_dt.isoformat()


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


# 枚举
class TaskStatus(StrEnum):
    """外部可见状态。

    持久化时必须落成字符串列（varchar），**不要**用数据库原生 enum 类型：
    将来增删状态值（例如把确认门加回来）才不需要写迁移。见 spec S2。
    """

    PENDING = "pending"  # 已创建/已重排队，等待执行槽
    RUNNING = "running"  # 执行中
    CANCELLING = "cancelling"  # 已受理取消，等 runner 走到检查点
    SUCCEEDED = "succeeded"  # 完成，result / result_uri 就绪
    FAILED = "failed"  # 出错，error 有原因
    CANCELLED = "cancelled"  # 已取消

    @property
    def is_terminal(self) -> bool:
        """本次执行已结束。注意 FAILED 可被**显式 retry** 重新打开，见 states.py。"""
        return self in frozenset(
            {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        )

    @property
    def is_active(self) -> bool:
        """占用执行槽 / 理论上有后台协程在跑。"""
        return self in frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLING})


#  值对象
@dataclass(slots=True)
class TaskError:
    """结构化错误。`retriable` 决定执行器是重排队还是判死刑。"""

    code: str
    message: str
    retriable: bool = False
    detail: str | None = None  # traceback，给人看，不进 UI
    at: datetime = field(default_factory=lambda: Time.now())


@dataclass(slots=True)
class StageRecord:
    """阶段留痕：前端画进度条、事后查「卡在哪一步」都靠它。"""

    stage: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"  # running / succeeded / failed / cancelled
    note: str | None = None


@dataclass(slots=True)
class TaskEvent:
    """审计事件流。比在 Task 上堆字段更适合做可观测性与 SSE 推送。"""

    task_id: str
    seq: int
    at: datetime
    type: str  # created / transition / stage / progress / error
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# 主体
@dataclass(slots=True)
class Task:
    """任务记录。

    用 `slots=True` 的额外好处：`setattr(task, "reslut", x)` 这类拼写错误会当场
    `AttributeError`，而不是悄悄挂一个没人读的属性上去。
    """

    # 身份
    task_id: str
    kind: str
    owner_id: str | None = None  # 多租户/多用户隔离
    idempotency_key: str | None = None  # 防前端重复提交

    # 输入
    request: Any = None  # 原始请求；重跑只需它 + kind

    # 状态
    status: TaskStatus = TaskStatus.PENDING
    stage: str | None = None
    stage_history: list[StageRecord] = field(default_factory=list)

    # 产物
    context: dict[str, Any] = field(
        default_factory=dict
    )  # 阶段间中间态（必须可序列化）
    result: Any = None  # 小结果直接放
    result_uri: str | None = None  # 大文件放引用（OSS/本地路径）

    # 断点续跑：下次执行从哪个阶段开始（None = 从头）
    #
    # 由 executor 在**可重试失败**时写入当前 stage，StagePipeline 据此跳过
    # 已完成的阶段。对 RAG 入库链路这不是优化而是必要——embedding 阶段因模型
    # 服务 503 失败时，不该把已花掉几秒 CPU 的解析和分块整个重做一遍。
    resume_stage: str | None = None

    # 失败与重试
    error: TaskError | None = None
    attempts: int = 0
    max_attempts: int = 1

    # 给 UI
    progress: float = 0.0  # 0.0 ~ 1.0
    message: str = ""  # 一句话人话状态

    # 并发与租约
    version: int = 0  # 乐观锁
    worker_id: str | None = None  # 谁在跑；配合 heartbeat 回收僵尸任务

    # 时间
    created_at: datetime = field(default_factory=lambda: Time.now())
    updated_at: datetime = field(default_factory=lambda: Time.now())
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None

    # 序列化
    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 友好结构（Redis/DB 落库、API 返回都用它）。"""
        return _encode(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        d = dict(data)
        for name in _DT_FIELDS:
            if d.get(name):
                d[name] = _parse_dt(d[name])
        d["status"] = TaskStatus(d["status"])
        if d.get("error"):
            e = dict(d["error"])
            e["at"] = _parse_dt(e["at"])
            d["error"] = TaskError(**e)
        d["stage_history"] = [
            StageRecord(
                **{
                    **r,
                    "started_at": _parse_dt(r["started_at"]),
                    "finished_at": _parse_dt(r["finished_at"])
                    if r.get("finished_at")
                    else None,
                }
            )
            for r in d.get("stage_history", [])
        ]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def public_view(self) -> dict[str, Any]:
        """给前端的裁剪视图：不漏 traceback、worker_id 这类内部信息。"""
        d = self.to_dict()
        for k in ("worker_id", "version", "idempotency_key", "context"):
            d.pop(k, None)
        if self.error is not None:
            d["error"] = {"code": self.error.code, "message": self.error.message}
        return d


_DT_FIELDS = (
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "heartbeat_at",
)
FIELD_NAMES = frozenset(f.name for f in fields(Task))


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _parse_dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)
