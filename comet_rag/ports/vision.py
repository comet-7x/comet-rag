from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VisionDescriptionPort(Protocol):
    """把一张已编码图片转换为文本描述的模型边界。"""

    def describe(self, base64_data: str, media_type: str) -> str: ...

    async def adescribe(self, base64_data: str, media_type: str) -> str: ...


__all__ = ["VisionDescriptionPort"]
