from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from comet_rag.core.logging import logger
from comet_rag.ports import AsyncVectorStore, CollectionNotFound
from comet_rag.ports.document_store import DocumentStore, RevisionKey, RevisionStatus
from comet_rag.services.revision_activation import ActivationOutcome


@dataclass(frozen=True, slots=True)
class CleanupReport:
    completed: tuple[RevisionKey, ...]
    failed: tuple[RevisionKey, ...]


class RevisionGarbageCollector:
    """回收已停止写入的 revision；不参与激活事务，也不承担任务租约回收。

    superseded 结果必须来自 RevisionActivator。登记失败由调用方重试；登记成功后，
    retired/abandoned manifest 就是持久化候选队列。生产接线必须先排干该 revision 的
    写入，跨存储 GC 无法阻止已经发出的旧 upsert 在清理后迟到。
    """

    def __init__(
        self, *, document_store: DocumentStore, vector_store: AsyncVectorStore
    ) -> None:
        self._documents = document_store
        self._vectors = vector_store

    async def collect_superseded(self, outcome: ActivationOutcome) -> None:
        """精确回收调用方持有的 superseded 结果；失败保留原异常供独立阶段重试。"""
        if not outcome.superseded or outcome.revision.status not in (
            RevisionStatus.READY,
            RevisionStatus.RETIRED,
            RevisionStatus.PURGED,
        ):
            raise ValueError("只能回收 RevisionActivator 返回的 superseded 版本")
        await self.collect_inactive(outcome.revision.key)

    async def sweep(self, *, limit: int = 32) -> CleanupReport:
        """只扫描一批持久化候选，不循环扫空；单个失败不阻断其他版本。"""
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        candidates = await self._documents.list_cleanup_candidates(limit=limit)
        completed: list[RevisionKey] = []
        failed: list[RevisionKey] = []
        for key in candidates:
            try:
                await self.collect_inactive(key)
            except Exception:
                logger.exception(f"revision GC 失败，保留供重试 key={key}")
                failed.append(key)
            else:
                completed.append(key)
        return CleanupReport(tuple(completed), tuple(failed))

    async def collect_inactive(
        self,
        key: RevisionKey,
        *,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """按持久化意图精确重放，包括 purged 的迟到写补偿；不发现新的候选。

        调用方保留身份并安排对账。成功只证明本次核验时为空，不证明未来远端写入
        不会落库；不能用 complete 回执或 purged 墓碑跳过再次删除。
        """

        async def check() -> None:
            if checkpoint is not None:
                await checkpoint()

        await check()
        # 原子拒绝 active；退役/废弃后不可重新激活，避免先查 pointer 再删除的竞态。
        # 即使候选提供者错传 active，也必须在任何向量 IO 之前被 Store 拒绝。
        await self._documents.abandon_revision(key)
        await check()
        condition = {"source_id": key.source_id, "document_revision": key.revision_id}
        try:
            await self._vectors.delete(key.kb_id, filter=condition)
            await check()
            remaining = await self._vectors.count(key.kb_id, filter=condition)
        except CollectionNotFound:
            # 只认可明确的不存在；连接/权限/超时错误不能当成空集合。
            remaining = 0
        if remaining != 0:
            raise RuntimeError(f"revision GC 仍有 {remaining} 个子向量 key={key}")
        # 向量删除/核验失败时绝不写 purged，manifest 才能留在下次扫描中。
        await check()
        await self._documents.purge_inactive_revision(key)
        await check()
