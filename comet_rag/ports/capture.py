from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from comet_rag.ports.document_store import RevisionKey
from comet_rag.ports.evidence import AsyncEvidenceStore, EvidenceBundle, evidence_digest


@dataclass(frozen=True, slots=True)
class SourceLock:
    """由受控预检/复核建立的来源事实；页数不采信MinerU或入库请求metadata。"""

    kb_id: str
    source_id: str
    source_sha256: str
    size: int
    pages: int
    filename: str
    approval_id: str

    def __post_init__(self) -> None:
        RevisionKey(self.kb_id, self.source_id, "source-lock")
        evidence_digest(self.source_id)
        evidence_digest(self.source_sha256)
        if type(self.size) is not int or not 0 < self.size <= 100 * 1024 * 1024:
            raise ValueError("来源锁字节数须在100MiB以内")
        if type(self.pages) is not int or not 0 < self.pages <= 512:
            raise ValueError("来源锁物理页数须在1..512以内")
        if not isinstance(self.filename, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,123}\.pdf", self.filename
        ):
            raise ValueError("来源锁需要安全PDF文件名")
        if not isinstance(self.approval_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.approval_id
        ):
            raise ValueError("来源锁需要受控预检的审批身份")

    def payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> SourceLock:
        if not isinstance(value, dict) or set(value) != {
            "kb_id",
            "source_id",
            "source_sha256",
            "size",
            "pages",
            "filename",
            "approval_id",
        }:
            raise ValueError("来源锁字段无效")
        return cls(**value)


class CaptureObserver(Protocol):
    """原生采集在副作用边界调用；回调失败必须阻止后续POST/对象发布。"""

    async def before_submit(self) -> None: ...

    async def task_observed(self, task_id: str) -> None: ...

    async def source_verified(self, task_id: str, source_file_id: str) -> None: ...


class AsyncEvidenceCapture(Protocol):
    @property
    def endpoint_sha256(self) -> str: ...

    async def capture(
        self,
        source: Path,
        lock: SourceLock,
        key: RevisionKey,
        *,
        observer: CaptureObserver,
        task_id: str | None,
    ) -> EvidenceBundle: ...


class AsyncEvidencePublisher(AsyncEvidenceStore, Protocol):
    @property
    def probe_key(self) -> str: ...

    @property
    def identity_sha256(self) -> str: ...

    def require_publish_permission(self) -> None:
        """只检查已验收的许可，不在每个任务里偷偷写入能力探测对象。"""
        ...

    async def verify_target(self) -> None: ...
