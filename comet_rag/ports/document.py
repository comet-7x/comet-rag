from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """提取器映射到通用字段、尚未执行跨格式规范化的结果。"""

    markdown: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """可直接交给 Chunker 的规范 Markdown 与文档元数据。"""

    markdown: str
    metadata: dict[str, object] = field(default_factory=dict)


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


__all__ = [
    "DocumentExtractionError",
    "DocumentExtractorPort",
    "DocumentProtocolError",
    "DocumentResourceLimitExceeded",
    "DocumentUpstreamError",
    "ExtractedDocument",
    "NormalizedDocument",
    "RetryableDocumentUpstreamError",
]
