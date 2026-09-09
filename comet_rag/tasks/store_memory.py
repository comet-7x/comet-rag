"""TaskStore 的进程内实现。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import Any

from comet_rag.core.time import Time

from .models import Task, TaskEvent, TaskStatus
from .store import TaskNotFound, TaskStore, VersionConflict


def _clone(task: Task) -> Task:
    """深拷可变字段，避免读取结果绕过 CAS 修改内存存储。"""
    copy = replace(task)
    copy.context = deepcopy(task.context)
    copy.request = deepcopy(task.request)
    copy.result = deepcopy(task.result)
    copy.stage_history = [replace(r) for r in task.stage_history]
    # `TaskError` 不是 frozen 的，`replace(task)` 只复制了引用。字段全是标量，
    # 一层 `replace` 就够，不必 deepcopy。
    copy.error = None if task.error is None else replace(task.error)
    return copy


class InMemoryTaskStore(TaskStore):
    """进程内任务表（重启即丢）。仅用于开发/测试/单机。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, list[TaskEvent]] = {}
        self._lock = asyncio.Lock()

    async def _insert(self, task: Task) -> tuple[Task, bool]:
        async with self._lock:
            if task.idempotency_key:
                for existing in self._tasks.values():
                    if (
                        existing.kind == task.kind
                        and existing.idempotency_key == task.idempotency_key
                    ):
                        return _clone(existing), False
            self._tasks[task.task_id] = _clone(task)
            return _clone(task), True

    async def _load(self, task_id: str) -> Task | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            return _clone(task) if task else None

    async def _save(
        self, task: Task, expected_version: int, *, bump: bool = True
    ) -> Task:
        async with self._lock:
            current = self._tasks.get(task.task_id)
            if current is None:
                raise TaskNotFound(task.task_id)
            if current.version != expected_version:
                raise VersionConflict(
                    f"任务 {task.task_id} 版本 {current.version} ≠ 期望 {expected_version}"
                )
            task.version = expected_version + 1 if bump else expected_version
            self._tasks[task.task_id] = _clone(task)
            return _clone(task)

    async def _remove(self, task_id: str) -> bool:
        async with self._lock:
            self._events.pop(task_id, None)
            return self._tasks.pop(task_id, None) is not None

    async def _query(
        self,
        *,
        kind: str | None = None,
        status: TaskStatus | None = None,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        async with self._lock:
            rows = [
                t
                for t in self._tasks.values()
                if (kind is None or t.kind == kind)
                and (status is None or t.status is status)
                and (owner_id is None or t.owner_id == owner_id)
                and (idempotency_key is None or t.idempotency_key == idempotency_key)
            ]
        rows.sort(key=lambda t: t.created_at, reverse=True)
        return [_clone(t) for t in rows[offset : offset + limit]]

    async def _append_event(
        self,
        task_id: str,
        type: str,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> TaskEvent:
        async with self._lock:
            bucket = self._events.setdefault(task_id, [])
            event = TaskEvent(
                task_id=task_id,
                seq=len(bucket) + 1,
                at=Time.now(),
                type=type,
                message=message,
                data=data or {},
            )
            bucket.append(event)
            return event

    async def events(self, task_id: str, *, after_seq: int = 0) -> list[TaskEvent]:
        async with self._lock:
            return [e for e in self._events.get(task_id, []) if e.seq > after_seq]
