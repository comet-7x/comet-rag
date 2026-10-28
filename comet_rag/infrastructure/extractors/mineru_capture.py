from __future__ import annotations

import tempfile
from pathlib import Path

from comet_rag.engines.offload import run_sync
from comet_rag.infrastructure.extractors.mineru_v1 import (
    AsyncMinerUV1Collector,
    CaptureSource,
)
from comet_rag.ports.capture import CaptureObserver, SourceLock
from comet_rag.ports.document_store import RevisionKey
from comet_rag.ports.evidence import EvidenceBundle


class AsyncMinerUEvidenceCapture:
    """生产采集Port的文件边界；临时字节归本次调用，远端身份归持久化journal。"""

    def __init__(
        self,
        collector: AsyncMinerUV1Collector,
        *,
        temporary_directory: Path | None = None,
    ) -> None:
        self._collector = collector
        self._temporary = temporary_directory

    @property
    def endpoint_sha256(self) -> str:
        return self._collector.endpoint_sha256

    async def capture(
        self,
        source: Path,
        lock: SourceLock,
        key: RevisionKey,
        *,
        observer: CaptureObserver,
        task_id: str | None,
    ) -> EvidenceBundle:
        capture_source = CaptureSource(
            source, lock.filename, lock.source_sha256, lock.size, lock.pages
        )
        temporary = await run_sync(
            tempfile.TemporaryDirectory, prefix="comet-mineru-", dir=self._temporary
        )
        try:
            return await self._collector.collect_bundle(
                capture_source,
                Path(temporary.name) / "capture",
                key,
                observer=observer,
                task_id=task_id,
            )
        finally:
            await run_sync(temporary.cleanup)
