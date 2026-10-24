from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from comet_rag.core.logging import logger
from comet_rag.ports.document_store import (
    ActiveRevisionDeletion,
    DocumentStore,
    RevisionConflict,
    RevisionStatus,
)
from comet_rag.services.revision_context import (
    PublicationPhase,
    load_publication,
    load_recovery_draft,
)
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.tasks import (
    Task,
    TaskBusy,
    TaskContext,
    TaskCursor,
    TaskStatus,
    TaskStore,
)
from comet_rag.tasks.runner import TaskCancelled
from comet_rag.tasks.store import TaskLeaseLost

_REVISION_MARKERS = frozenset(
    {"revision_key", "revision_prepare", "revision_index", "revision_publication"}
)
_SCAN_KEY = "revision_scan"


class RevisionTaskRetained(TaskBusy):
    """仍是持续对账的恢复来源，不能按普通任务 TTL 或 force 删除。"""


def guard_revision_task_deletion(task: Task) -> None:
    # 连不完整或损坏的意图也要保留；complete、终态和年龄都不是远端排干证据。
    if (_REVISION_MARKERS | {_SCAN_KEY}).intersection(task.context):
        raise RevisionTaskRetained(
            f"任务 {task.task_id} 保留 revision 恢复身份，禁止删除"
        )


class ReconciliationDisposition(StrEnum):
    CLEANED = "cleaned"
    DEFERRED = "deferred"
    IGNORED = "ignored"
    MISSING_PAYLOAD = "missing_payload"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    scanned: int
    cleaned: tuple[str, ...]
    deferred: tuple[str, ...]
    ignored: tuple[str, ...]
    failed: tuple[str, ...]
    cycle_complete: bool
    # 与仍在执行/可重试的 deferred 分开；这类记录需要外部恢复证据，等待本身无效。
    missing_payload: tuple[str, ...] = ()


class RevisionReconciler:
    """按保留的任务身份持续对账；不发布版本、不重试业务、不启动后台扫描。

    调用方提供由执行器拥有的扫描 TaskContext，并安排后续页/轮次。每次最多处理一页，
    一次只加载一条 Task，避免 page_size 份 32 MiB context 同时驻留。失败项保留在源表，
    下一轮重访；扫描者取消或丢失所有权时不越过当前项。这个游标不承诺数据库快照。
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        document_store: DocumentStore,
        garbage_collector: RevisionGarbageCollector,
        task_kind: str,
        page_size: int = 8,
        timeout_seconds: float = 30,
    ) -> None:
        if not isinstance(task_kind, str) or not task_kind:
            raise ValueError("task_kind 必须为非空字符串")
        # 工作量上限与时间预算独立；不从失败页循环到扫空。
        if type(page_size) is not int or not 1 <= page_size <= 32:
            raise ValueError("page_size 必须为 1～32 的整数")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds 必须为有限正数")
        self._tasks = task_store
        self._documents = document_store
        self._gc = garbage_collector
        self._kind = task_kind
        self._page_size = page_size
        self._timeout = timeout_seconds

    def _cursor(self, context: dict[str, object]) -> TaskCursor | None:
        if _SCAN_KEY not in context:
            return None
        value = context[_SCAN_KEY]
        if not isinstance(value, dict) or set(value) != {"version", "kind", "before"}:
            raise ValueError("扫描游标结构错误")
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or value["kind"] != self._kind
        ):
            raise ValueError("扫描游标版本或任务范围不匹配")
        before = value["before"]
        if before is None:
            return None
        if (
            not isinstance(before, dict)
            or set(before) != {"created_at", "task_id"}
            or not all(isinstance(v, str) for v in before.values())
        ):
            raise ValueError("扫描锚点结构错误")
        return TaskCursor(
            datetime.fromisoformat(before["created_at"]), before["task_id"]
        )

    async def _save_cursor(self, ctx: TaskContext, cursor: TaskCursor | None) -> None:
        # 专用扫描上下文只容纳游标；CAS 内复验预算和执行器所有权。
        await ctx.put_bounded(
            4096,
            **{
                _SCAN_KEY: {
                    "version": 1,
                    "kind": self._kind,
                    "before": {
                        "created_at": cursor.created_at.isoformat(),
                        "task_id": cursor.task_id,
                    }
                    if cursor
                    else None,
                }
            },
        )

    async def reconcile(
        self, task: Task, *, checkpoint: Callable[[], Awaitable[None]] | None = None
    ) -> ReconciliationDisposition:
        if task.kind != self._kind:
            raise ValueError("对账任务范围不匹配")
        if not _REVISION_MARKERS.intersection(task.context):
            return ReconciliationDisposition.IGNORED
        if not task.status.is_terminal:
            return ReconciliationDisposition.DEFERRED
        draft = load_recovery_draft(task.context, task.task_id)
        if draft is None:
            # 第十九批之前的缺载荷旧记录没有可靠计划身份，仍需迁移/人工恢复。
            logger.warning(
                f"event=revision_reconciliation_missing_prepare task_id={task.task_id}"
            )
            return ReconciliationDisposition.MISSING_PAYLOAD
        publication = load_publication(task.context, draft)
        if checkpoint is not None:
            await checkpoint()
        manifest = await self._documents.reserve_revision(draft)
        if publication is not None and publication.generation != manifest.generation:
            raise RevisionConflict(draft.key, "对账 generation 与 manifest 不一致")
        eligible = manifest.status in (
            RevisionStatus.RETIRED,
            RevisionStatus.ABANDONED,
            RevisionStatus.PURGED,
        )
        if manifest.status is RevisionStatus.READY and publication is not None:
            eligible = publication.phase in (
                PublicationPhase.CLEANUP_PENDING,
                PublicationPhase.CLEANUP_COMPLETE,
            )
        # CANCELLED 不可被 retry 重开；FAILED 则仍须保留 staging/ready 供原业务续跑。
        if task.status is TaskStatus.CANCELLED and manifest.status in (
            RevisionStatus.STAGING,
            RevisionStatus.READY,
        ):
            eligible = True
        if not eligible:
            return ReconciliationDisposition.DEFERRED
        try:
            await self._gc.collect_inactive(draft.key, checkpoint=checkpoint)
        except ActiveRevisionDeletion:
            # 并发激活先提交时，Store 必须拒绝 abandon，不能据旧快照删除 active。
            return ReconciliationDisposition.DEFERRED
        return ReconciliationDisposition.CLEANED

    async def run_page(self, ctx: TaskContext) -> ReconciliationReport:
        if not ctx.has_ownership_fence:
            raise ValueError("扫描上下文必须绑定 worker_id 和 attempts 所有权围栏")
        async with asyncio.timeout(self._timeout):
            await ctx.checkpoint()
            cursor = self._cursor((await ctx.snapshot()).context)
            await self._save_cursor(ctx, cursor)
            cleaned: list[str] = []
            deferred: list[str] = []
            ignored: list[str] = []
            failed: list[str] = []
            missing_payload: list[str] = []
            scanned = 0
            for _ in range(self._page_size):
                await ctx.checkpoint()
                rows = await self._tasks.list_tasks_before(
                    kind=self._kind, before=cursor, limit=1
                )
                if not rows:
                    await self._save_cursor(ctx, None)
                    return ReconciliationReport(
                        scanned,
                        tuple(cleaned),
                        tuple(deferred),
                        tuple(ignored),
                        tuple(failed),
                        True,
                        tuple(missing_payload),
                    )
                if len(rows) != 1:
                    raise ValueError("TaskStore 未遵守单条分页上限")
                task = rows[0]
                if task.kind != self._kind:
                    raise ValueError("TaskStore 返回了其他任务范围的记录")
                following = TaskCursor(task.created_at, task.task_id)
                if cursor is not None and (following.created_at, following.task_id) >= (
                    cursor.created_at,
                    cursor.task_id,
                ):
                    raise ValueError("TaskStore 返回了已越过的分页锚点")
                try:
                    disposition = await self.reconcile(task, checkpoint=ctx.checkpoint)
                except (TaskCancelled, TaskLeaseLost):
                    raise
                except Exception:
                    logger.exception(
                        f"revision 单项对账失败，保留供下一轮重试 task_id={task.task_id}"
                    )
                    failed.append(task.task_id)
                else:
                    if disposition is ReconciliationDisposition.CLEANED:
                        cleaned.append(task.task_id)
                    elif disposition is ReconciliationDisposition.DEFERRED:
                        deferred.append(task.task_id)
                    elif disposition is ReconciliationDisposition.MISSING_PAYLOAD:
                        missing_payload.append(task.task_id)
                    else:
                        ignored.append(task.task_id)
                # 顺序不可提前；IO 中断或确认响应丢失时，新 Context 从持久化锚点恢复。
                await self._save_cursor(ctx, following)
                cursor = following
                scanned += 1
                del rows, task
            return ReconciliationReport(
                scanned,
                tuple(cleaned),
                tuple(deferred),
                tuple(ignored),
                tuple(failed),
                False,
                tuple(missing_payload),
            )
