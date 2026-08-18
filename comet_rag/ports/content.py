"""模型调用共享的媒体、内容与重排值对象。

这些类型不包含 OpenAI/Qwen 字段，也不负责网络请求。适配器在基础设施边界把
它们转换成各自的请求 DTO。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class MediaResource:
    """一份媒体资源的明确来源。

    ``data``、``path``、``url`` 必须且只能提供一个。与把三种来源都塞进
    ``image_url: str`` 相比，调用处和适配器都无需猜测字符串代表什么。
    """

    data: bytes | None = None
    path: Path | None = None
    url: str | None = None
    mimetype: str | None = None

    def __post_init__(self) -> None:
        sources = (self.data is not None, self.path is not None, self.url is not None)
        if sum(sources) != 1:
            raise ValueError("MediaResource 必须且只能设置 data、path、url 之一")
        if self.data is not None and not self.data:
            raise ValueError("MediaResource.data 不能为空")
        if self.path is not None:
            object.__setattr__(self, "path", Path(self.path))
        if self.url is not None and not self.url.strip():
            raise ValueError("MediaResource.url 不能为空")
        if self.data is not None and not self.mimetype:
            raise ValueError("字节媒体必须显式提供 mimetype")


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("TextContent.text 不能为空")


@dataclass(frozen=True, slots=True)
class ImageContent:
    resource: MediaResource
    detail: Literal["auto", "low", "high"] = "auto"


type ContentPart = TextContent | ImageContent
type ContentInput = str | Sequence[ContentPart]


@dataclass(frozen=True, slots=True)
class RerankDocument:
    """一个待重排候选；保留业务标识和元数据，避免调用方手工按索引回填。"""

    content: ContentInput
    id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.content, str):
            if not self.content:
                raise ValueError("RerankDocument.content 不能为空")
            return
        parts = tuple(self.content)
        if not parts:
            raise ValueError("RerankDocument.content 不能为空")
        object.__setattr__(self, "content", parts)


@dataclass(frozen=True, slots=True)
class RankedDocument:
    """重排后的候选及其在原始输入中的位置。"""

    index: int
    score: float
    document: RerankDocument


__all__ = [
    "ContentInput",
    "ContentPart",
    "ImageContent",
    "MediaResource",
    "RankedDocument",
    "RerankDocument",
    "TextContent",
]
