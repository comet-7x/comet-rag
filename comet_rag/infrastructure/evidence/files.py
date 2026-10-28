from __future__ import annotations

from pathlib import Path


def read_limited(path: Path, limit: int) -> bytes:
    """文件和将来的流式HTTP都应先限字节；不先无界读取再检查。"""
    if path.stat().st_size > limit:
        raise ValueError("输入文件超出 wire 字节预算")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("输入文件超出 wire 字节预算")
    return raw
