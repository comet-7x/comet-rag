from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VisionDescriptionPort(Protocol):
    """同步把已编码图片转换为文本描述。"""

    def describe(self, base64_data: str, media_type: str) -> str: ...


@runtime_checkable
class AsyncVisionDescriptionPort(Protocol):
    """异步把已编码图片转换为文本描述。"""

    async def describe(self, base64_data: str, media_type: str) -> str: ...


__all__ = ["VisionDescriptionPort", "AsyncVisionDescriptionPort"]
