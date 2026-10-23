"""后端中立的标量过滤：字段间 AND，候选集合表示标量 IN。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

type FilterScalar = str | bool | int | float
type FilterValue = (
    FilterScalar | list[FilterScalar] | tuple[FilterScalar, ...] | set[FilterScalar]
)
type Filter = Mapping[str, FilterValue]

# JSON 的跨 int/float 比较可能转成 double；此范围避免整数舍入产生假命中。
MAX_FILTER_NUMBER = 2**53 - 1


def _scalar(value: object) -> FilterScalar:
    if type(value) not in (str, bool, int, float):
        raise ValueError("Filter 值必须是字符串、布尔或有限数值；不支持 null/对象")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Filter 数值必须有限")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not -MAX_FILTER_NUMBER <= value <= MAX_FILTER_NUMBER
    ):
        raise ValueError("Filter 数值超出跨后端安全范围 ±(2**53-1)")
    return cast("FilterScalar", value)


def normalize_filter(condition: object) -> Filter:
    """完整验证并复制输入；空过滤匹配全部，空 IN 匹配零条。

    缺失/null/数组/对象不能匹配标量谓词。null 作为查询值拒绝，而非猜成 IS NULL。
    IN 候选必须同一 Python 标量类型，bool 与数字分离；数字相等允许 int/float 互比。
    """
    if condition is None:
        return {}
    if not isinstance(condition, Mapping):
        raise ValueError("Filter 必须是字段到标量或候选集合的映射")
    normalized: dict[str, FilterValue] = {}
    for key, value in condition.items():
        if not isinstance(key, str) or not key:
            raise ValueError("Filter 字段必须是非空字符串")
        if isinstance(value, (list, tuple, set)):
            items = [_scalar(item) for item in value]
            if len({type(item) for item in items}) > 1:
                raise ValueError("Filter IN 候选必须同类型")
            normalized[key] = items
        else:
            normalized[key] = _scalar(value)
    return normalized


def _equal(actual: object, expected: FilterScalar) -> bool:
    if type(expected) is bool:
        return type(actual) is bool and actual == expected
    if type(expected) is str:
        return type(actual) is str and actual == expected
    return type(actual) in (int, float) and actual == expected


def matches_filter(metadata: Mapping[str, object], condition: Filter | None) -> bool:
    """解释已经验证的 Filter；存储入口负责先验证整棵条件，不能按命中与否跳过校验。"""
    for key, expected in (condition or {}).items():
        if key not in metadata:
            return False
        actual = metadata[key]
        candidates = (
            expected if isinstance(expected, (list, tuple, set)) else [expected]
        )
        if not any(_equal(actual, candidate) for candidate in candidates):
            return False
    return True
