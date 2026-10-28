from __future__ import annotations

from comet_rag.services.evidence_publication import guard_publication_task_deletion
from comet_rag.services.flat_write import guard_flat_write_deletion
from comet_rag.services.mineru_capture import guard_capture_task_deletion
from comet_rag.services.revision_reconciliation import guard_revision_task_deletion
from comet_rag.tasks import Task


def guard_task_deletion(task: Task) -> None:
    """普通TTL/force不能删除唯一的revision、来源写入或MinerU恢复身份。"""
    guard_revision_task_deletion(task)
    guard_flat_write_deletion(task)
    guard_capture_task_deletion(task)
    guard_publication_task_deletion(task)
