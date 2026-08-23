"""带时区的时间工厂。

## 为什么在 `core/` 而不是 `tasks/`

它原本住在 `tasks/models.py` 里，而 `infrastructure/knowledge_base.py` 只为了
取个当前时间就得 import `comet_rag.tasks.models` —— 与 `tasks/store_postgres.py`
反过来 import `infrastructure.database` 一起，构成了包级**循环依赖**。

一个只依赖标准库的时间工具跟"任务"没有任何关系，把它放进任务包纯属历史巧合。
挪到零依赖内核后环就断了：`infrastructure` 不再依赖 `tasks`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from enum import StrEnum
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
        """ISO 8601 字符串。传了 `dt` 就把它**转换**到 `tz`，而不是照原样输出。

        原先写的是 `dt or cls.now(tz)` —— 传 `dt` 时 `tz` 被静默丢弃，
        `iso(dt, TimeZone.UTC)` 返回的仍是 `dt` 自己的时区。参数在那里、
        被接受、然后不起作用，比没有这个参数更糟。
        """
        return (cls.to(dt, tz) if dt is not None else cls.now(tz)).isoformat()


__all__ = ["TZType", "Time", "TimeZone"]
