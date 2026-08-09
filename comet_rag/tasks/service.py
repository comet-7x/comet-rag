"""TaskService：store + executor 的组合门面，API 层只跟它打交道。

存在的意义是把「必须成对发生」的动作封在一起：
提交 = 建记录 + 排期；取消 = 改状态 + 掐协程；重试 = 重开状态 + 重新排期。
散在路由里迟早会漏掉其中一半。
"""

from __future__ import annotations

from typing import Any

from .executor import TaskExecutor
from .models import Task, TaskEvent, TaskStatus
from .store import TaskStore


class TaskService:
    def __init__(self, store: TaskStore, executor: TaskExecutor) -> None:
        self.store = store
        self.executor = executor

    # 提交与查询
    async def submit(
        self,
        kind: str,
        request: Any = None,
        *,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 1,
        **fields: Any,
    ) -> Task:
        task = await self.store.create(
            kind,
            request=request,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            **fields,
        )
        if task.status is TaskStatus.PENDING and task.attempts == 0:
            await self.executor.submit(task.task_id)
        return await self.store.require(task.task_id)

    async def get(self, task_id: str) -> Task | None:
        return await self.store.get(task_id)

    async def list(
        self,
        *,
        kind: str | None = None,
        status: TaskStatus | None = None,
        owner_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        return await self.store.list_tasks(
            kind=kind, status=status, owner_id=owner_id, limit=limit, offset=offset
        )

    async def events(self, task_id: str, *, after_seq: int = 0) -> list[TaskEvent]:
        return await self.store.events(task_id, after_seq=after_seq)

    # 控制
    async def cancel(self, task_id: str) -> bool:
        """请求取消。True = 已受理，**不等于**已停；要确认请轮询 status.is_terminal。"""
        return await self.executor.request_cancel(task_id)

    async def retry(
        self, task_id: str, *, reset_attempts: bool = True, from_scratch: bool = False
    ) -> Task:
        """把失败的任务重新打开。

        默认保留 `context` 与 `resume_stage`，从失败的那个阶段续跑。
        `from_scratch=True` 则清空续跑锚点，整条流水线从头重来 ——
        当怀疑是前置阶段产出有问题（而非单纯的下游抖动）时用它。
        """
        task = await self.store.require(task_id)
        if task.status is not TaskStatus.FAILED:
            raise ValueError(f"只有 FAILED 可重试，当前 {task.status.value}")
        fields: dict[str, Any] = {"error": None, "message": "已重新排队"}
        if reset_attempts:
            fields["attempts"] = 0
        if from_scratch:
            fields["resume_stage"] = None
        await self.store.transition(
            task_id, TaskStatus.PENDING, note="人工重试", **fields
        )
        await self.executor.submit(task_id)
        return await self.store.require(task_id)

    async def delete(self, task_id: str, *, force: bool = False) -> bool:
        if force:
            await self.executor.request_cancel(task_id)
        return await self.store.delete(task_id, force=force)
