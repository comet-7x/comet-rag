"""`TaskStore` 的进程内实现。

只实现 7 个存取原语，业务规则（状态机守卫、时间戳维护、租约回收、事件留痕）
全在基类的模板方法里 —— 与 `store_postgres.py` 同理，两者跑同一套契约测试。

## 它不是"简化版"，是**同等严肃**的一个实现

开发、单测、以及单进程部署都跑在它上面。所以 CAS、版本号、事件序号一个都不
能省：省掉的那部分行为差异，会在换到 Postgres 时才暴露出来，而那时排查成本
高得多。`_clone` 尤其如此 —— 真实数据库天然隔离（一次序列化/反序列化），
内存实现必须自己把这件事做到。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import Any

from comet_rag.core.time import Time

from .models import Task, TaskEvent, TaskStatus
from .store import TaskNotFound, TaskStore, VersionConflict


def _clone(task: Task) -> Task:
    """拷出一份**独立**快照。真实 DB 实现这里是一次序列化/反序列化。

    `request`、`result` 与 `context` 里的嵌套值都必须真拷。原来只做
    `dict(task.context)` 这种浅拷、且完全没碰 `request`/`result`，于是：

        got = await store.get(task_id)
        got.request["source"] = "别的东西"     # 没有 save、没有 CAS

    这行就**直接改到了库里**：version 不涨、不留事件、别人手上的快照当场
    与库不一致，而乐观锁一无所知 —— 它守的是 `_save`，这条路根本没经过
    `_save`。`_load` 的文档说"返回副本，返回活对象会让调用方绕过 CAS 直接
    改库"，而它自己就是那个活对象。

    `request` 与 `result` 声明成 `Any`，实际装的几乎总是 dict（入库请求、
    结果摘要），正是最容易被顺手改一下的两个字段。

    deepcopy 对不可变值（str、int、datetime）会原样返回，所以代价与容器的
    **元素个数**成正比，与内容大小无关；chunk 列表那种场景不会因此变慢。
    """
    copy = replace(task)
    copy.context = deepcopy(task.context)
    copy.request = deepcopy(task.request)
    copy.result = deepcopy(task.result)
    copy.stage_history = [replace(r) for r in task.stage_history]
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
