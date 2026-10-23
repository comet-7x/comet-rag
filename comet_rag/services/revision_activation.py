from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from comet_rag.ports.document_store import (
    DocumentStore,
    RevisionDraft,
    RevisionNotReady,
    RevisionStatus,
    StaleRevision,
    StoredRevision,
)


@dataclass(frozen=True, slots=True)
class ActivationOutcome:
    """一次激活尝试的结果快照；不承诺返回之后仍是当前版本。"""

    revision: StoredRevision
    superseded: bool = False
    already_active: bool = False


def _was_replaced(revision: StoredRevision) -> bool:
    # activated_at 区分曾成功发布的版本与从未发布就被清理的版本。
    return revision.activated_at is not None and revision.status in (
        RevisionStatus.RETIRED,
        RevisionStatus.PURGED,
    )


class RevisionActivator:
    """只发布已 ready 的 manifest；写入、激活与 GC 各自保留独立重试边界。

    网络异常无法证明事务未提交，必须原样交给调用方重试同一 draft。禁止因此回滚
    active 或删除向量。尚未激活的旧 generation 保持 ready，后续 GC 批次负责废弃。
    """

    def __init__(self, document_store: DocumentStore) -> None:
        self._documents = document_store

    async def activate(
        self,
        draft: RevisionDraft,
        /,
        *,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
    ) -> ActivationOutcome:
        async def check() -> None:
            if checkpoint is not None:
                await checkpoint()

        await check()
        # 幂等 reserve 既校验计划/任务身份，也读取已提交状态；不重新分配 generation。
        manifest = await self._documents.reserve_revision(draft)
        await check()
        if _was_replaced(manifest):
            return ActivationOutcome(manifest, superseded=True)
        if manifest.status not in (RevisionStatus.READY, RevisionStatus.ACTIVE):
            raise RevisionNotReady(draft.key, f"当前状态为 {manifest.status}")
        try:
            activation = await self._documents.activate_revision(draft.key)
        except StaleRevision:
            return ActivationOutcome(manifest, superseded=True)
        except RevisionNotReady:
            # reserve 与激活之间可能被更新版本替换；只恢复有持久化发布证据的版本。
            await check()
            latest = await self._documents.reserve_revision(draft)
            await check()
            if _was_replaced(latest):
                return ActivationOutcome(latest, superseded=True)
            raise
        return ActivationOutcome(
            activation.active, already_active=activation.already_active
        )
