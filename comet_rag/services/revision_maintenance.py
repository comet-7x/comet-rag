from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from typing import TypedDict
from uuid import uuid4

from comet_rag.core.logging import logger
from comet_rag.core.time import Time
from comet_rag.services.revision_reconciliation import (
    ReconciliationReport,
    RevisionReconciler,
)
from comet_rag.tasks import Task, TaskContext, TaskError, TaskStatus, TaskStore
from comet_rag.tasks.runner import TaskCancelled
from comet_rag.tasks.store import TaskLeaseLost, VersionConflict

RECONCILIATION_KIND = "revision-reconciliation"
RECONCILIATION_OVERDUE_SECONDS = 180


class MaintenanceStatus(TypedDict):
    task_id: str
    status: str
    updated_at: str
    overdue: bool
    last_page: object
    error_code: str | None


class RevisionMaintenance:
    """一个持久化游标，每次定时触发只认领一页；不创建队列或后台协程。

    该 Task 是维护游标，不进入业务执行队列。TaskStore CAS 排他认领，唯一 invocation
    身份与单调 attempts 围栏防止旧调用收尾覆盖新调用；跨进程失联复用已有租约回收。
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        reconciler: RevisionReconciler,
        task_kind: str,
        resource_identity: str,
    ) -> None:
        if (
            not isinstance(task_kind, str)
            or not task_kind.strip()
            or task_kind == RECONCILIATION_KIND
        ):
            raise ValueError("对账来源 kind 必须非空且不能为维护任务自身")
        if not isinstance(resource_identity, str) or not resource_identity:
            raise ValueError("对账必须绑定存储命名空间")
        self._tasks = task_store
        self._reconciler = reconciler
        self._kind = task_kind
        self._request = {
            "version": 1,
            "kind": task_kind,
            "resource": sha256(resource_identity.encode()).hexdigest(),
        }
        self._task_id: str | None = None

    def _scope(self, task: Task) -> None:
        if (
            task.kind != RECONCILIATION_KIND
            or not isinstance(task.request, dict)
            or type(task.request.get("version")) is not int
            or task.request != self._request
        ):
            raise ValueError("对账游标已绑定其他存储命名空间或来源 kind")

    async def initialize(self) -> None:
        # key 只按来源 kind 分配；同一 TaskStore 中切换向量命名空间必须被拒绝，不能另建
        # 一个游标悄悄扫描相同来源。request 只保存摘要，不持久化连接串或凭据。
        task = await self._tasks.create(
            RECONCILIATION_KIND,
            idempotency_key="revision-scan:" + sha256(self._kind.encode()).hexdigest(),
            request=self._request,
            context={
                "revision_scan": {"version": 1, "kind": self._kind, "before": None}
            },
            max_attempts=1,
        )
        self._scope(task)
        self._task_id = task.task_id

    async def _task(self) -> Task:
        if self._task_id is None:
            raise RuntimeError("对账维护尚未初始化")
        task = await self._tasks.require(self._task_id)
        self._scope(task)
        return task

    async def status(self) -> MaintenanceStatus:
        task = await self._task()
        last_page_at = task.created_at
        if isinstance(task.result, dict) and isinstance(task.result.get("at"), str):
            last_page_at = datetime.fromisoformat(task.result["at"])
            if last_page_at.utcoffset() is None:
                raise ValueError("对账页确认时间必须含时区")
        return {
            "task_id": task.task_id,
            "status": task.status.value,
            "updated_at": task.updated_at.isoformat(),
            # 认领、重试与心跳不代表完成一页，不能用 updated_at 掩盖持续无进展。
            "overdue": task.status is not TaskStatus.CANCELLED
            and (Time.now() - last_page_at).total_seconds()
            > RECONCILIATION_OVERDUE_SECONDS,
            "last_page": task.result,
            "error_code": task.error.code if task.error else None,
        }

    async def _release(
        self,
        claim: Task,
        status: TaskStatus,
        *,
        report: ReconciliationReport | None = None,
        error: TaskError | None = None,
    ) -> None:
        def guard(current: Task) -> None:
            self._scope(current)
            if (
                not current.status.is_active
                or current.worker_id != claim.worker_id
                or current.attempts != claim.attempts
            ):
                raise TaskLeaseLost(claim.task_id)
            if (
                current.status is TaskStatus.CANCELLING
                and status is not TaskStatus.CANCELLED
            ):
                raise TaskCancelled(claim.task_id)

        await self._tasks._transition_guarded(
            claim.task_id,
            status,
            guard=guard,
            worker_id=None,
            error=error,
            result=(
                {"at": Time.now().isoformat(), **asdict(report)}
                if report is not None
                else claim.result
            ),
        )

    async def run_once(self) -> ReconciliationReport | None:
        task = await self._task()
        if task.status not in (TaskStatus.PENDING, TaskStatus.FAILED):
            # CANCELLED 是显式停用，不创建替代任务；活跃记录由现有租约回收处理。
            return None
        try:
            if task.status is TaskStatus.FAILED:
                logger.warning(
                    f"event=revision_reconciliation_retry task_id={task.task_id}"
                )
                task = await self._tasks.transition(
                    task.task_id, TaskStatus.PENDING, expected_version=task.version
                )
            claim = await self._tasks.transition(
                task.task_id,
                TaskStatus.RUNNING,
                expected_version=task.version,
                worker_id="revision-scan-" + uuid4().hex,
                attempts=task.attempts + 1,
                error=None,
            )
        except VersionConflict:
            return None
        ctx = TaskContext(
            self._tasks,
            claim.task_id,
            worker_id=claim.worker_id,
            attempts=claim.attempts,
        )
        try:
            report = await self._reconciler.run_page(ctx)
            await self._release(claim, TaskStatus.PENDING, report=report)
        except TaskCancelled:
            await self._release(claim, TaskStatus.CANCELLED)
            return None
        except TaskLeaseLost:
            raise
        except Exception as exc:
            logger.exception(
                f"event=revision_reconciliation_failed task_id={claim.task_id}"
            )
            await self._release(
                claim,
                TaskStatus.FAILED,
                error=TaskError(
                    code="revision_reconciliation_failed",
                    message=f"对账页失败（{type(exc).__name__}）",
                    retriable=True,
                ),
            )
            raise
        # 外部 CancelledError 不写假成功、不启动脱离关停的清理；已提交游标留待租约恢复。
        if report.failed:
            logger.error(
                f"event=revision_reconciliation_partial_failure task_id={claim.task_id} "
                f"failed={len(report.failed)}"
            )
        logger.info(
            f"event=revision_reconciliation_page task_id={claim.task_id} "
            f"scanned={report.scanned} cycle_complete={report.cycle_complete}"
        )
        return report
