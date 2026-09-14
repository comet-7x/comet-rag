from __future__ import annotations

from collections.abc import Callable
from operator import index

LengthFunction = Callable[[str], int]


def measure(length_function: LengthFunction, text: str) -> int:
    try:
        value = index(length_function(text))
    except TypeError:
        raise TypeError("length_function 必须返回整数") from None
    if value < 0:
        raise ValueError("length_function 不能返回负数")
    return value


def validate_sizing(
    chunk_size: int,
    chunk_overlap: int,
    length_function: LengthFunction,
) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap 必须大于等于 0")
    if chunk_overlap >= chunk_size:
        raise ValueError("重叠字符数 chunk_overlap 必须小于 chunk_size")
    if measure(length_function, "") != 0:
        raise ValueError("length_function 对空字符串必须返回 0")


def max_end_within_budget(
    text: str,
    start: int,
    end: int,
    budget: int,
    length_function: LengthFunction,
) -> int:
    """返回从 start 起不超过预算的最远字符位置。

    二分查找依赖长度函数对前缀单调不减；这是 tokenizer/字符/字节计数器共同满足的
    最小契约。单个 Unicode code point 已超预算时明确失败，不能产生超长块。
    """

    if start >= end:
        return start
    if measure(length_function, text[start:end]) <= budget:
        return end
    if measure(length_function, text[start : start + 1]) > budget:
        raise ValueError(
            f"单个字符在 length_function 下已超过 chunk_size={budget}"
        )

    low, high = start + 1, end
    best = start + 1
    while low <= high:
        middle = (low + high) // 2
        if measure(length_function, text[start:middle]) <= budget:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def earliest_suffix_within_budget(
    text: str,
    start: int,
    end: int,
    budget: int,
    length_function: LengthFunction,
) -> int:
    if budget == 0:
        return end
    if measure(length_function, text[start:end]) <= budget:
        return start

    low, high = start, end
    best = end
    while low <= high:
        middle = (low + high) // 2
        if measure(length_function, text[middle:end]) <= budget:
            best = middle
            high = middle - 1
        else:
            low = middle + 1
    return best


__all__ = ["LengthFunction"]
