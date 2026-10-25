from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

TitlePolicy = Literal["plain", "headings", "ancestors"]
MAX_HEADING_DEPTH = 6
MAX_HEADING_CHARS = 80
MAX_PATH_CHARS = 256


def title_view(text: str, metadata: Mapping[str, object], policy: TitlePolicy) -> str:
    """仅构造模型视图；超限不截断实体，缺失/损坏的结构事实回退原文。"""
    if policy not in ("plain", "headings", "ancestors"):
        raise ValueError("未知标题上下文策略")
    if policy == "plain" or metadata.get("block_kind") != "section":
        return text
    path = metadata.get("heading_path")
    if not isinstance(path, (list, tuple)) or not 1 <= len(path) <= MAX_HEADING_DEPTH:
        return text
    if any(
        not isinstance(part, str) or not part.strip() or len(part) > MAX_HEADING_CHARS
        for part in path
    ):
        return text
    if sum(len(part) for part in path) > MAX_PATH_CHARS:
        return text
    selected = path if policy == "headings" else path[:-1]
    if not selected:
        return text
    return "标题路径（仅定位信息）：" + " / ".join(selected) + "\n\n正文：\n" + text
