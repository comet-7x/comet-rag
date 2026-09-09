"""集中生成带时区时间，便于测试替换时钟。"""

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

        传入 `dt` 时也必须转换到 `tz`，否则时区参数会被静默忽略。
        """
        return (cls.to(dt, tz) if dt is not None else cls.now(tz)).isoformat()


__all__ = ["TZType", "Time", "TimeZone"]
