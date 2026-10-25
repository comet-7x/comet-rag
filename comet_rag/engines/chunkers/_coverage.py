from __future__ import annotations

from collections.abc import Iterable


def validate_coverage(source: str, spans: Iterable[tuple[int, int]]) -> None:
    """调用方已验证单个 span；这里检查顺序与非空正文覆盖，允许 overlap 和空白间隔。"""
    previous_start = -1
    covered_end = 0
    for start, end in spans:
        if start < previous_start:
            raise ValueError("Chunker span 必须按文档位置排列")
        if source[covered_end:start].strip():
            raise ValueError("分块遗漏非空原文")
        covered_end = max(covered_end, end)
        previous_start = start
    if source[covered_end:].strip():
        raise ValueError("分块遗漏非空原文")
