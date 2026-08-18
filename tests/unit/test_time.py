"""`core.time.Time` 的时区语义。"""

from __future__ import annotations

from datetime import datetime

from comet_rag.core.time import Time, TimeZone


def test_iso_converts_the_given_datetime_into_the_requested_zone() -> None:
    """**传了 `dt` 时 `tz` 必须真的生效。**

    原实现是 `dt or cls.now(tz)`：传 `dt` 时 `tz` 被静默丢弃，输出仍是 `dt`
    自己的时区。参数在那里、被接受、然后不起作用 —— 比没有这个参数更糟，
    因为调用方会以为自己指定了时区。
    """
    beijing = Time.now(TimeZone.CST).replace(
        year=2026, month=1, day=1, hour=8, minute=0, second=0, microsecond=0
    )

    as_utc = Time.iso(beijing, TimeZone.UTC)

    assert as_utc.startswith("2026-01-01T00:00:00"), as_utc
    assert datetime.fromisoformat(as_utc) == beijing


def test_iso_without_datetime_uses_the_requested_zone_now() -> None:
    assert Time.iso(tz=TimeZone.UTC).endswith("+00:00")
