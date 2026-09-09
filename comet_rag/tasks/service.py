"""TaskService：store + executor 的组合门面，API 层只跟它打交道。

存在的意义是把「必须成对发生」的动作封在一起：
提交 = 建记录 + 排期；取消 = 改状态 + 掐协程；重试 = 重开状态 + 重新排期。
散在路由里迟早会漏掉其中一半。
"""

from __future__ import annotations

from typing import Any

from comet_rag.core.logging import logger

from .executor import TaskExecutor
from .models import Task, TaskEvent, TaskStatus
from .store import TaskStore

#: 这几种状态意味着"这个任务已经有人管了或已经有结果了"，此时 `submit` 被拒
#: 是良性的竞态，不是错误。**逐一列出而不是写成 `!= PENDING`**：
#: 日后状态机加了新状态，会掉进 `raise` 分支被看见，而不是被默默咽掉。
_ALREADY_HANDLED = frozenset(
    {
        TaskStatus.RUNNING,  # 别的 worker 抢先认领了 —— 最常见的一种
        TaskStatus.CANCELLING,  # 已在取消途中，此时排期毫无意义
        TaskStatus.CANCELLED,  # 用户已取消，不该再排
        TaskStatus.SUCCEEDED,  # 已经跑完了
        TaskStatus.FAILED,  # 跑过并判死（如 kind 未注册），排期的目的已达成
    }
)


class Backlogged(RuntimeError):
    """待执行任务已堆到上限，拒收新任务（spec S4-1）。

    **明确拒绝，不静默丢弃**：API 层翻译成 429，客户端知道该退避重来。
    不设这道界的话，投递量一大队列就无限堆积 —— 表面上"全都收下了"，
    实际是把 OOM 和"排队两小时"往后推。
    """


class TaskService:
    def __init__(
        self,
        store: TaskStore,
        executor: TaskExecutor,
        *,
        max_backlog: int = 0,
    ) -> None:
        self.store = store
        self.executor = executor
        #: 待执行任务上限，0 = 不限（单进程/当库用时的默认）
        self._max_backlog = max_backlog

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
        await self._check_backlog()
        task = await self.store.create(
            kind,
            request=request,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            **fields,
        )
        if task.status is TaskStatus.PENDING and task.attempts == 0:
            await self._enqueue(task.task_id)
        return await self.store.require(task.task_id)

    async def backlog(self) -> dict[str, Any]:
        """当前积压。给 `/admin/limits` 用 —— 限流是否生效不该靠猜。

        `pending` 最多数到上限为止：积压很深时全表计数本身就是最慢的查询，
        而运维只需要知道"到顶了没有"。
        """
        cap = self._max_backlog or 1000
        pending = await self.store.list_tasks(status=TaskStatus.PENDING, limit=cap)
        return {
            "pending": len(pending),
            "at_least": len(pending) >= cap,  # 到了上限就说明可能还有更多
            "max_backlog": self._max_backlog or None,
        }

    async def _check_backlog(self) -> None:
        """创建记录前做有界查询；该准入检查允许并发提交造成短暂超额。"""
        if not self._max_backlog:
            return
        pending = await self.store.list_tasks(
            status=TaskStatus.PENDING, limit=self._max_backlog
        )
        if len(pending) >= self._max_backlog:
            raise Backlogged(
                f"待执行任务已达上限 {self._max_backlog}，暂不受理新任务，请稍后重试"
            )

    async def _enqueue(self, task_id: str) -> None:
        """仅忽略任务已被其他提交者或 worker 接手的竞态。"""
        try:
            await self.executor.submit(task_id)
        except ValueError:
            current = await self.store.get(task_id)
            if current is None or current.status not in _ALREADY_HANDLED:
                raise
            logger.info(
                f"任务 {task_id} 在入队前已被接手（当前 {current.status.value}），"
                f"本次投递跳过"
            )

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
