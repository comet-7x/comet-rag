from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """提取器映射到通用字段、尚未执行跨格式规范化的结果。"""

    markdown: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    """引用规范 Markdown 区间的结构事实，不保存第二份正文。"""

    kind: str
    ordinal: int
    start_char: int
    end_char: int
    heading_path: tuple[str, ...] = ()
    page_number: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("DocumentBlock.kind 必须是非空字符串")
        if not _is_int(self.ordinal):
            raise TypeError("DocumentBlock.ordinal 必须是 int")
        if self.ordinal < 0:
            raise ValueError("DocumentBlock.ordinal 必须大于等于 0")
        if not _is_int(self.start_char) or not _is_int(self.end_char):
            raise TypeError("DocumentBlock 字符位置必须是 int")
        if self.start_char < 0:
            raise ValueError("DocumentBlock.start_char 必须大于等于 0")
        if self.end_char <= self.start_char:
            raise ValueError("DocumentBlock.end_char 必须大于 start_char")
        if self.page_number is not None:
            if not _is_int(self.page_number):
                raise TypeError("DocumentBlock.page_number 必须是 int | None")
            if self.page_number <= 0:
                raise ValueError("DocumentBlock.page_number 必须大于 0")
        if not isinstance(self.heading_path, tuple) or any(
            not isinstance(heading, str) for heading in self.heading_path
        ):
            raise TypeError("DocumentBlock.heading_path 必须是 tuple[str, ...]")
        if any(not isinstance(key, str) or not key for key in self.metadata):
            raise ValueError("DocumentBlock.metadata 键必须是非空字符串")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata))
        )


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """可交给文档级 ChunkingStrategy 的规范 Markdown 与文档元数据。"""

    markdown: str
    metadata: dict[str, object] = field(default_factory=dict)
    blocks: tuple[DocumentBlock, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.markdown, str):
            raise TypeError("NormalizedDocument.markdown 必须是 str")
        blocks = tuple(self.blocks)
        previous_end = 0
        for expected, block in enumerate(blocks):
            if not isinstance(block, DocumentBlock):
                raise TypeError(
                    f"NormalizedDocument.blocks 第 {expected} 项必须是 DocumentBlock"
                )
            if block.ordinal != expected:
                raise ValueError("DocumentBlock.ordinal 必须从 0 严格连续")
            if block.end_char > len(self.markdown):
                raise ValueError("DocumentBlock span 超出 NormalizedDocument.markdown")
            if block.start_char < previous_end:
                raise ValueError("DocumentBlock span 必须按顺序排列且不能重叠")
            previous_end = block.end_char
        object.__setattr__(self, "blocks", blocks)


class DocumentExtractionError(RuntimeError):
    """所有文档提取失败的共同边界。"""


class DocumentProtocolError(DocumentExtractionError):
    """上游成功响应不符合已约定的提取协议。"""


class DocumentResourceLimitExceeded(DocumentExtractionError):
    """输入、响应或提取结果超过本地资源预算。"""


class DocumentUpstreamError(DocumentExtractionError):
    """上游明确拒绝请求，默认不能靠原样重试恢复。"""


class RetryableDocumentUpstreamError(DocumentUpstreamError):
    """网络故障、限流或服务端错误，调用方可按任务策略重试。"""


@runtime_checkable
class DocumentExtractorPort(Protocol):
    """从受管本地文件生成通用提取结果的最小契约。

    Loader 负责把 Local、URL、S3 来源变成本地文件；此接口因此不接受 URL、
    凭据或供应商参数，避免来源路由与内容提取重新耦合。
    """

    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument: ...

    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument: ...

    async def aclose(self) -> None:
        """释放实现持有的资源；不应关闭由调用方注入的资源。"""
        ...


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "DocumentExtractionError",
    "DocumentBlock",
    "DocumentExtractorPort",
    "DocumentProtocolError",
    "DocumentResourceLimitExceeded",
    "DocumentUpstreamError",
    "ExtractedDocument",
    "NormalizedDocument",
    "RetryableDocumentUpstreamError",
]
