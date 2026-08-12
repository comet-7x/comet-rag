"""执行器：把 PENDING 任务真正推起来。

与 TaskStore 分家的理由见 store.py 顶部。这里的接口只收 **task_id**，不收协程——
执行器自己按 kind 去注册表取 runner，用 request/context 重建执行。
于是「重试」「进程重启后恢复」「失败后从断点续跑」都变成同一个动作：`submit(task_id)`。

**取消语义（务必让调用方知道）**：`request_cancel` 返回 True 只代表*已受理*，
不代表*已停止*。RUNNING 任务先被打成 CANCELLING，真正落 CANCELLED 要等 runner
走到下一个 `ctx.checkpoint()`。查 `task.status.is_terminal` 才是「真停了」。

## 三层结构

    TaskExecutor          纯接口，调用方只看得到 submit / request_cancel / shutdown
    StoreDrivenExecutor   「一个 task_id 该怎么被执行」—— 进程内与跨进程共用
    InProcessExecutor     asyncio 版：本地协程 + 信号量闸门

进程内与跨进程执行器的**真差别只有两点**：任务在哪儿被拉起、重试怎么重排队。
状态机推进、超时、失败分类、断点续跑锚点这些是同一件事，抄成两份必然会漂——
而"漂"的症状是"换个部署方式行为就变了"，那正是本项目要消灭的东西。
"""

from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from typing import Any

from .models import TaskError, TaskStatus
from .runner import (
    Done,
    Handoff,
    Outcome,
    RetriableError,
    TaskCancelled,
    TaskContext,
    get_runner,
)
from .store import TaskStore

logger = logging.getLogger("app.task")


class TaskExecutor(ABC):
    """运行时调度接口。进程内 asyncio、线程池、Celery/RQ 各出一个实现。"""

    @abstractmethod
    async def submit(self, task_id: str, *, delay: float = 0.0) -> None: ...

    @abstractmethod
    async def request_cancel(self, task_id: str) -> bool: ...

    @abstractmethod
    async def shutdown(self, *, timeout: float | None = 10.0) -> None: ...


class StoreDrivenExecutor(TaskExecutor):
    """执行器的共用骨架：**只经 TaskStore 观察与推进任务**。

    子类要补的只有三件事：

      * `submit()`      —— 把 task_id 送到该去的地方（本地协程 / Redis 队列）
      * `_schedule_retry()` —— 可重试失败后怎么重排队
      * `shutdown()`    —— 各自的资源怎么释放

    其余（RUNNING 抢占、超时、成功收尾、失败分类、续跑锚点、取消受理）
    都在这里，两种部署形态跑的是同一份代码。
    """

    def __init__(
        self,
        store: TaskStore,
        *,
        default_timeout: float | None = None,
        retry_backoff: float = 1.0,
        worker_id: str | None = None,
        worker_prefix: str = "exec",
        lane: str | None = None,
    ) -> None:
        self._store = store
        self._timeout = default_timeout
        self._backoff = retry_backoff
        self._worker_id = worker_id or f"{worker_prefix}-{uuid.uuid4().hex[:6]}"
        #: 本实例服务哪条负载道。None = 全都做（单进程），此时永远不会有移交。
        self._lane = lane
        #: 本实例正在执行的任务。关停时要把它们排干，否则会留下 RUNNING 僵尸。
        self._inflight: set[str] = set()
        self._detached: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def lane(self) -> str | None:
        return self._lane

    # ── 执行 ───────────────────────────────────────────────────────────────

    async def execute(self, task_id: str) -> None:
        """把一个任务从 PENDING 推到终态。**进程内与跨进程共用这一份。**

        是 public 的：跨进程实现的 worker 侧入口（arq 的 job 函数）要调它，
        而那已经算不上"内部"了。
        """
        self._inflight.add(task_id)
        try:
            # 排队期间可能已被取消/删除/被别的 worker 抢走，进来先复查
            task = await self._store.get(task_id)
            if task is None or task.status is not TaskStatus.PENDING:
                return
            runner = get_runner(task.kind)  # 未注册的 kind 直接走失败分支
            await self._store.transition(
                task_id,
                TaskStatus.RUNNING,
                attempts=task.attempts + 1,
                worker_id=self._worker_id,
                error=None,
                message="执行中",
            )
            ctx = TaskContext(
                self._store, task_id, worker_id=self._worker_id, lane=self._lane
            )
            coro = runner(ctx)
            outcome = (
                await asyncio.wait_for(coro, self._timeout)
                if self._timeout
                else await coro
            )
            await self._finish(task_id, outcome)
        except asyncio.CancelledError:
            # 已经在被取消的协程里再 await 是不可靠的，把收尾工作甩给独立协程
            self._detach(self._mark_cancelled(task_id))
            raise
        except TaskCancelled:
            await self._mark_cancelled(task_id)
        except Exception as exc:  # noqa: BLE001 —— runner 的任何异常都在这里收口
            await self._mark_failed(task_id, exc)
        finally:
            self._inflight.discard(task_id)

    @abstractmethod
    async def _schedule_retry(self, task_id: str, delay: float) -> None:
        """可重试失败后重新排队。**不得在此阻塞等待 delay**——
        进程内实现要立刻返回（否则本轮协程收不了尾），跨进程实现用队列的延迟投递。
        """

    async def _schedule_handoff(self, task_id: str, lane: str) -> None:
        """把任务交到另一条负载道上。

        默认退化成"就地重排队"：单进程部署里根本不分道，也就不会走到这儿；
        真要走到了（比如有人给 InProcessExecutor 配了 lane），重排一次总比
        把任务丢了强。跨进程实现覆写它，投到目标道对应的队列。
        """
        await self._schedule_retry(task_id, 0.0)

    # ── 收尾 ───────────────────────────────────────────────────────────────

    async def _finish(self, task_id: str, outcome: Outcome) -> None:
        if isinstance(outcome, Handoff):
            await self._hand_off(task_id, outcome)
            return
        if not isinstance(outcome, Done):
            raise TypeError(
                f"runner 必须返回 Done 或 Handoff，得到 {type(outcome).__name__}"
            )
        await self._store.transition(
            task_id,
            TaskStatus.SUCCEEDED,
            result=outcome.result,
            result_uri=outcome.result_uri,
            # 成功即清空续跑锚点：否则这条任务日后被显式 retry 时
            # 会从半途开始，而不是完整重跑。
            resume_stage=None,
            message=outcome.message,
        )

    async def _hand_off(self, task_id: str, outcome: Handoff) -> None:
        """交棒：退回 PENDING + 写续跑锚点 + 投到目标道。

        **不消耗重试次数**。`execute()` 进 RUNNING 时把 attempts 加了 1，
        那笔账要在这里退回去 —— 移交是正常流程，不是一次失败的尝试。
        不退的话，一条三阶段两次移交的流水线开跑即耗掉 2 次重试预算，
        真出故障时反而没得重试了。
        """
        task = await self._store.get(task_id)
        if task is None or task.status is not TaskStatus.RUNNING:
            return  # 期间被取消了，别把它拽回 PENDING
        await self._store.transition(
            task_id,
            TaskStatus.PENDING,
            attempts=max(task.attempts - 1, 0),
            resume_stage=outcome.resume_stage,
            message=outcome.message,
            note=f"移交 {self._lane} → {outcome.lane}",
        )
        logger.info(
            "任务 %s 移交 %s → %s（续跑自 %s）",
            task_id,
            self._lane,
            outcome.lane,
            outcome.resume_stage,
        )
        await self._schedule_handoff(task_id, outcome.lane)

    async def _mark_cancelled(self, task_id: str) -> None:
        """落 CANCELLED。**幂等**：任务已是终态就直接返回。

        取消路径上可能有多个来源同时想收尾（协程被 cancel + runner 自己
        抛 TaskCancelled），让它幂等比让调用方去协调便宜得多。
        """
        task = await self._store.get(task_id)
        if task is None or task.status.is_terminal:
            return
        await self._store.transition(
            task_id,
            TaskStatus.CANCELLED,
            error=TaskError(code="cancelled", message="任务被取消"),
            message="已取消",
        )

    async def _mark_failed(self, task_id: str, exc: BaseException) -> None:
        err = _to_task_error(exc)
        task = await self._store.get(task_id)
        if task is None or task.status.is_terminal:
            return

        # 断点续跑锚点：记下失败发生在哪个阶段，下次从这里重来而非从头。
        # 两个分支都要写 —— 判死那条也写，是为了让日后的**显式 retry**
        # 同样能续跑。非流水线 runner 的 stage 为 None，写入无副作用。
        resume_stage = task.stage

        if err.retriable and task.attempts < task.max_attempts:
            delay = self._backoff * (2 ** (task.attempts - 1))
            await self._store.transition(
                task_id,
                TaskStatus.PENDING,
                error=err,
                resume_stage=resume_stage,
                message=f"第 {task.attempts} 次失败，{delay:.3g}s 后重试",
                note="可重试失败，重排队",
            )
            await self._schedule_retry(task_id, delay)
            return

        logger.warning("任务 %s 失败：%s", task_id, err.message)
        await self._store.transition(
            task_id,
            TaskStatus.FAILED,
            error=err,
            resume_stage=resume_stage,
            message="执行失败",
        )

    # ── 取消 ───────────────────────────────────────────────────────────────

    async def _accept_cancel(self, task_id: str) -> bool:
        """取消请求的**状态侧**受理，两种执行器完全一致。

        跨进程时这就是取消的全部——`asyncio.Task.cancel()` 传不过进程边界，
        只能靠 runner 在 `ctx.checkpoint()` 里看见 CANCELLING 自己退出。
        进程内实现在此之上还会补一刀本地 cancel，让卡在长 await 上的 runner
        立刻醒过来。
        """
        task = await self._store.get(task_id)
        if task is None or task.status.is_terminal:
            return False
        if task.status is TaskStatus.RUNNING:
            await self._store.transition(
                task_id, TaskStatus.CANCELLING, message="正在取消…"
            )
        elif task.status is not TaskStatus.CANCELLING:
            # PENDING：没有真在跑的 runner，可以直接落终态
            await self._store.transition(
                task_id,
                TaskStatus.CANCELLED,
                error=TaskError(code="cancelled", message="任务被主动取消"),
                message="已取消",
            )
        return True

    async def request_cancel(self, task_id: str) -> bool:
        """受理取消。任务不存在或已是终态 → False。"""
        return await self._accept_cancel(task_id)

    # ── 生命周期 ───────────────────────────────────────────────────────────

    def _detach(self, coro: Coroutine[Any, Any, None]) -> None:
        """跑一段「必须完成、但不能被外层取消牵连」的收尾协程，并持引用防 GC。"""
        task = asyncio.ensure_future(coro)
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    async def wait_all(self) -> None:
        """等本实例的收尾协程与在跑任务都结束（测试 / 优雅关停用）。"""
        while self._detached:
            pending = [t for t in self._detached if not t.done()]
            if not pending:
                # 已结束但 done-callback 还没轮到执行，让出一轮即可被摘掉
                await asyncio.sleep(0)
                continue
            await asyncio.gather(*pending, return_exceptions=True)


class InProcessExecutor(StoreDrivenExecutor):
    """进程内 asyncio 执行器：信号量限并发 + 超时 + 可重试失败退避重排队。"""

    def __init__(
        self,
        store: TaskStore,
        *,
        max_concurrency: int = 4,
        default_timeout: float | None = None,
        retry_backoff: float = 1.0,
        hard_cancel: bool = True,
        worker_id: str | None = None,
        lane: str | None = None,
    ) -> None:
        super().__init__(
            store,
            default_timeout=default_timeout,
            retry_backoff=retry_backoff,
            worker_id=worker_id,
            worker_prefix="inproc",
            # 默认 None = 不分道，整条流水线在本进程跑完。单进程部署下
            # 分道毫无意义 —— 移交只会变成一次没有必要的入队往返。
            lane=lane,
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._hard_cancel = hard_cancel  # 是否在协作取消之外再补一刀 asyncio.cancel
        self._bg: dict[str, asyncio.Task[None]] = {}

    # 提交
    async def submit(self, task_id: str, *, delay: float = 0.0) -> None:
        if self._closed:
            raise RuntimeError("执行器已关停，拒绝新任务")
        task = await self._store.require(task_id)
        if task.status is not TaskStatus.PENDING:
            raise ValueError(f"只有 PENDING 可提交，当前 {task.status.value}")
        running = self._bg.get(task_id)
        if running is not None and not running.done():
            return  # 幂等：重复 submit 不会跑两遍
        bg = asyncio.create_task(
            self._run(task_id, delay), name=f"{task.kind}:{task_id}"
        )
        self._bg[task_id] = bg
        bg.add_done_callback(self._on_done)

    async def _run(self, task_id: str, delay: float) -> None:
        """延迟 + 并发闸门，然后交给共用的 `execute()`。

        闸门在**这一层**而不在 `execute()` 里：跨进程实现的闸门属于 worker
        （arq 的 `max_jobs`），生产端不该也无法限流消费端。
        """
        try:
            if delay:
                await asyncio.sleep(delay)
            async with self._sem:
                await self.execute(task_id)
        except asyncio.CancelledError:
            # 在拿到闸门之前就被取消：execute() 还没接手，这里补收尾。
            # `_mark_cancelled` 幂等，与 execute() 内那次重复也无害。
            self._detach(self._mark_cancelled(task_id))
            raise

    async def _schedule_retry(self, task_id: str, delay: float) -> None:
        # 不能在这里直接 submit：此刻**自己**还挂在 self._bg[task_id] 上，
        # 会被 submit 的幂等守卫挡掉。交给一个独立协程等本轮收尾后再排。
        self._detach(self._resubmit_later(task_id, delay))

    async def _resubmit_later(self, task_id: str, delay: float) -> None:
        """退避重排队：先等本轮协程彻底收尾，再计时重投。"""
        bg = self._bg.get(task_id)
        if bg is not None and not bg.done():
            await asyncio.wait({bg})
        await asyncio.sleep(delay)
        task = await self._store.get(task_id)
        if task is None or task.status is not TaskStatus.PENDING or self._closed:
            return  # 期间被取消/删除/关停
        try:
            await self.submit(task_id)
        except Exception:  # noqa: BLE001
            logger.exception("重排队失败：%s", task_id)

    # 取消
    async def request_cancel(self, task_id: str) -> bool:
        accepted = await self._accept_cancel(task_id)
        if accepted and self._hard_cancel:
            self._cancel_bg(task_id)
        return accepted

    def _cancel_bg(self, task_id: str) -> None:
        """掐掉在跑的协程，让卡在长 await 上的 runner 立刻醒过来。

        ⚠️ `asyncio.Task.cancel()` **不是线程安全的**（这条是原稿里最值钱的注释）：
        FastAPI 会把 `def`（非 async）端点丢到 AnyIO worker 线程执行，在那儿直接 cancel，
        非 debug 模式下是未定义行为，开 asyncio debug 则直接 RuntimeError 且取消不生效。
        本包全异步后端点必然在事件循环线程上，这条路基本走不到；但保留探测，
        以防有人从线程池里调过来。
        """
        bg = self._bg.get(task_id)
        if bg is None or bg.done():
            return
        loop = bg.get_loop()
        try:
            on_loop_thread = asyncio.get_running_loop() is loop
        except RuntimeError:  # 压根不在事件循环线程里
            on_loop_thread = False
        if on_loop_thread:
            bg.cancel()
        else:
            loop.call_soon_threadsafe(bg.cancel)

    # 生命周期
    def _on_done(self, bg: asyncio.Task[None]) -> None:
        """解绑 + 兜底记录异常。

        业务失败已被 `_mark_failed` 写进 task.error，走不到这里；这里接住的是
        **执行器/runner 自身的 bug**——否则 asyncio 只在 GC 时打一句
        "Task exception was never retrieved"，极难定位。
        """
        for task_id, running in list(self._bg.items()):
            if running is bg:
                del self._bg[task_id]
                break
        if bg.cancelled():
            logger.info("后台任务 %s 已取消", bg.get_name())
            return
        exc = bg.exception()
        if exc is not None:
            logger.error("后台任务 %s 未捕获异常: %r", bg.get_name(), exc, exc_info=exc)

    async def wait_all(self) -> None:
        """等所有后台协程收尾（测试 / 优雅关停用）。

        ⚠️ 只 gather **尚未结束**的协程。已经结束但 done-callback 还没轮到执行的
        协程仍留在 `_bg` 里，对它们 gather 会把事件循环堵死（连外层 wait_for 都超不了时）。
        这种情况只需 `sleep(0)` 让出一轮，callback 就会把它们摘掉。
        """
        while self._bg or self._detached:
            pending = [t for t in (*self._bg.values(), *self._detached) if not t.done()]
            if not pending:
                await asyncio.sleep(0)
                continue
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self, *, timeout: float | None = 10.0) -> None:
        """停止收新任务；先协作取消在跑的，再等它们收尾，超时则硬掐。"""
        self._closed = True
        for task_id in list(self._bg):
            await self.request_cancel(task_id)
        try:
            await asyncio.wait_for(self.wait_all(), timeout)
        except TimeoutError:
            logger.warning("关停超时，硬取消剩余 %d 个后台协程", len(self._bg))
            for bg in list(self._bg.values()):
                bg.cancel()
            await self.wait_all()


def _to_task_error(exc: BaseException) -> TaskError:
    if isinstance(exc, RetriableError):
        return TaskError(
            code=exc.code, message=str(exc), retriable=True, detail=_tb(exc)
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return TaskError(
            code="timeout", message="执行超时", retriable=True, detail=_tb(exc)
        )
    return TaskError(
        code=type(exc).__name__, message=str(exc) or type(exc).__name__, detail=_tb(exc)
    )


def _tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
