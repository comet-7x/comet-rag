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
        """积压到上限就拒收。**在建记录之前**查 —— 建完再拒等于白建一条。

        只查 `max_backlog` 条而不是 count(*)：判断"满没满"只需要知道有没有
        第 N 条，全表计数在积压很深时反而是最慢的那个查询。

        判据是 `>=` 而非 `>`：积压已经到 N 了，再收一条就是 N+1，
        那就**超过**了上限。写成 `>` 的话实际容量是 N+1（实测差这一条）。

        ## 这道界是 best-effort，不是硬不变式（PR 评审 #6）

        查与建之间没有事务，所以 N 个并发提交可以都读到"还没满"然后都插入 ——
        瞬时积压最多冲到 `max_backlog + 并发提交数`。**这是刻意接受的**：

          · 它的职责是**准入控制**，防的是"投递量远超处理能力时无限堆积"，
            而不是给出一个精确到个位的容量数字；超出量以并发数为界，
            不随投递总量增长 —— 内存有界这个目标仍然成立。
          · 换成事务化配额需要一张全局计数行，于是**每一次提交都要抢同一把锁**。
            那正是本项目在事件序号上踩过的坑（见 `store_postgres.py` 顶部）：
            所有写入者争抢同一个资源时，冲突是必然而非偶然。
            为一道模糊边界付出全局串行的代价，不划算。

        真需要精确上限时，正确的位置是**入口层的限流器**（网关/中间件按 QPS
        限），而不是把任务表改造成一个分布式信号量。
        """
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
        """排期，并咽下"有人抢先一步"这一种失败。

        跨进程部署下，「读到 PENDING」与「真正入队」之间隔着一次网络往返，
        worker 完全可能在这个缝里把任务捞走。此时 `executor.submit` 会以
        "只有 PENDING 可提交" 拒绝 —— 但调用方的目的（任务被排上了）其实
        已经达成，报错反而会让一次幂等的重复 POST 变成 500。

        只在**确认它真的离开了 PENDING** 时才咽：否则就是真错误，必须上抛。

        关于"会不会把不相干的错误也咽掉"（PR 评审 #10）：
          · 执行器已关停抛的是 `RuntimeError`，**不在捕获范围内**，照常上抛；
          · 任务被并发取消 → 状态离开 PENDING → 咽掉是对的：它已经不该被排期了；
          · 任何让它留在 PENDING 的失败 → 上抛。
        也就是说，被咽掉的只有"别人已经接手了"这一种。
        但**咽掉必须留痕** —— 原来是完全静默的，真出了没预料到的情况也查不出来。
        """
        try:
            await self.executor.submit(task_id)
        except ValueError:
            current = await self.store.get(task_id)
            if current is None or current.status is TaskStatus.PENDING:
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
