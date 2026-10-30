from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from comet_rag.pipelines.types import IngestionResult


class CometRAGError(Exception):
    """库自定义异常的共同基类；不包含所有参数、SDK 或系统异常。"""


class SourceError(CometRAGError, ValueError):
    """来源获取或完整性校验失败；消息不包含认证定位。"""


class SourceResourceLimitError(SourceError):
    """交付字节或上游声明大小超过来源预算。"""


class ArchiveResourceLimitError(CometRAGError, ValueError):
    """压缩容器超过允许的资源预算。"""


class IngestionIncompleteError(CometRAGError, RuntimeError):
    """内容清单已提交，向量状态需按相同身份核对后才能切换。"""

    def __init__(self, result: IngestionResult) -> None:
        super().__init__(
            f"vector state unknown or incomplete for {result.manifest.manifest_id}"
        )
        self.result = result
