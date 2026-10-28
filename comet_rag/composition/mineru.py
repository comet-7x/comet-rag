from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass

from comet_rag.config.schemas import APPConfig
from comet_rag.ports.document_store import DocumentStore
from comet_rag.services.evidence_access import EvidenceReader
from comet_rag.services.mineru_capture import MinerUWorkflow
from comet_rag.tasks import TaskStore


@dataclass(frozen=True, slots=True)
class MinerUIntegration:
    """组合根的显式启用结果；服务默认不加载来源许可或创建文档/S3客户端。"""

    workflow: MinerUWorkflow
    reader: EvidenceReader | None = None


type MinerUFactory = Callable[
    [APPConfig, TaskStore, DocumentStore, AsyncExitStack],
    Awaitable[MinerUIntegration],
]


@dataclass(slots=True)
class MinerUResources:
    """工厂在当前事件循环内登记资源；失败归rollback，成功归Context逆序关闭。"""

    stack: AsyncExitStack

    async def close(self) -> None:
        await self.stack.aclose()
