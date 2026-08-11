"""Runner 侧：执行上下文、结果类型、kind→runner 注册表、多阶段流水线。

**断点续跑的关键取舍**（整个设计的分水岭）：
阶段间的中间态一律写进 `task.context` 并落库，而不是留在协程的局部变量里。
- 留局部变量更省事，但任务被绑死在进程的事件循环里，重启即丢，也无法跨机器接管；
- 落库写法要求 `context` 必须可序列化，换来的是：进程可随意重启、
  失败重试时由任何 worker 从 `resume_stage` 接手续跑，已完成的阶段不必重做。

注意 `StagePipeline` 与 `engines.pipelines.Pipeline` 是两回事：
前者编排**任务阶段**，后者编排**文档处理**。名字曾经撞过，故此处加 Stage 前缀。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from .models import Task, TaskStatus, Time
from .store import TaskStore


# 结果类型
@dataclass(slots=True)
class Done:
    """runner 正常结束。大产物请用 result_uri，别把二进制塞进 result。"""

    result: Any = None
    result_uri: str | None = None
    message: str = "已完成"


Outcome = Done


class TaskCancelled(Exception):
    """checkpoint 检测到取消请求时抛出，由执行器捕获并落成 CANCELLED。"""


class RetriableError(Exception):
    """runner 主动声明「这次失败可以重试」（网络抖动、上游限流等）。"""

    def __init__(self, message: str, *, code: str = "retriable") -> None:
        super().__init__(message)
        self.code = code


# 执行上下文
class TaskContext:
    """runner 与任务状态之间的唯一通道。

    runner 不直接碰 TaskStore：一来避免它乱改 status，二来将来换存储时
    runner 代码一行不用动。
    """

    def __init__(
        self,
        store: TaskStore,
        task_id: str,
        *,
        worker_id: str | None = None,
        heartbeat_interval: timedelta = timedelta(seconds=10),
    ) -> None:
        self.store = store
        self.task_id = task_id
        self.worker_id = worker_id
        self._hb_interval = heartbeat_interval
        self._last_beat = Time.now()

    async def snapshot(self) -> Task:
        """读当前任务快照（副本，改它不会影响库）。"""
        return await self.store.require(self.task_id)

    async def checkpoint(self) -> None:
        """协作式取消检查点 + 续租。

        `asyncio.Task.cancel()` 只在 await 点生效，跨进程更是完全无效，
        所以取消**必然**是协作式的：runner 需要在每个阶段边界、每轮长循环里
        调一次 checkpoint，看见 CANCELLING 就干净退出。
        """
        task = await self.snapshot()
        if task.status is TaskStatus.CANCELLING:
            raise TaskCancelled(self.task_id)
        now = Time.now()
        if now - self._last_beat >= self._hb_interval:
            self._last_beat = now
            await self.store.heartbeat(self.task_id)

    async def enter_stage(
        self, stage: str, *, progress: float | None = None, message: str = ""
    ) -> None:
        fields: dict[str, Any] = {}
        if progress is not None:
            fields["progress"] = progress
        if message:
            fields["message"] = message
        await self.store.enter_stage(self.task_id, stage, **fields)

    async def put(self, **values: Any) -> None:
        """往 context 里写中间态（必须 JSON 友好）。续跑就靠它。"""
        task = await self.snapshot()
        await self.store.update(self.task_id, context={**task.context, **values})

    async def report(
        self, *, progress: float | None = None, message: str | None = None
    ) -> None:
        fields: dict[str, Any] = {}
        if progress is not None:
            fields["progress"] = max(0.0, min(1.0, progress))
        if message is not None:
            fields["message"] = message
        if fields:
            await self.store.update(self.task_id, **fields)


class Runner(Protocol):
    """业务侧只需实现这一个签名。用 Protocol 是因为它由**外部业务模块**实现——
    不该逼着每个业务去 import 并继承一个基类。"""

    async def __call__(self, ctx: TaskContext) -> Outcome: ...


# 注册表
_REGISTRY: dict[str, Runner] = {}


class UnknownKind(KeyError):
    pass


def register(kind: str, *, replace: bool = False) -> Callable[[Runner], Runner]:
    """把 kind 与 runner 绑定。

    这是「submit(task_id) 而非 spawn(task, coro)」得以成立的关键：
    执行器只要有 task_id，就能用 kind 查到函数、用 request/context 重建执行，
    于是重跑、崩溃恢复、失败后断点续跑全都成立。

    默认拒绝重复注册 —— 静默覆盖会让「明明注册了却跑的是别人的 runner」
    极难排查。但**装配代码**需要幂等：runner 常是持有依赖的可调用对象
    （见 `services/ingestion.py`），应用每次启动都要重新绑定一次，
    此时用 `replace=True` 显式声明意图。
    """

    def deco(fn: Runner) -> Runner:
        if kind in _REGISTRY and not replace:
            raise ValueError(f"kind 重复注册：{kind}。装配代码请显式传 replace=True。")
        _REGISTRY[kind] = fn
        return fn

    return deco


def unregister(kind: str) -> None:
    """解绑。主要给测试用，避免用例间互相污染全局注册表。"""
    _REGISTRY.pop(kind, None)


def get_runner(kind: str) -> Runner:
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise UnknownKind(f"未注册的任务种类：{kind}") from None


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


# 多阶段流水线
StageFn = Callable[[TaskContext], Awaitable[Outcome | None]]


@dataclass(slots=True)
class StagePipeline:
    """按顺序跑一串命名阶段；支持从 `task.resume_stage` 断点续跑。

    阶段函数返回：
      * `None`  → 继续下一阶段
      * `Done`  → 提前结束整条流水线

    续跑语义：`resume_stage` 由执行器在**可重试失败**时写入当前阶段名，
    重试时本流水线跳过它之前的所有阶段，直接从失败处重来。被跳过阶段的
    产出必须已经写进 `task.context`（用 `ctx.put()`），否则续跑会读不到。
    """

    stages: list[tuple[str, StageFn]] = field(default_factory=list)

    def stage(self, name: str) -> Callable[[StageFn], StageFn]:
        def deco(fn: StageFn) -> StageFn:
            self.stages.append((name, fn))
            return fn

        return deco

    async def __call__(self, ctx: TaskContext) -> Outcome:
        names = [n for n, _ in self.stages]
        task = await ctx.snapshot()
        start = names.index(task.resume_stage) if task.resume_stage in names else 0
        total = len(self.stages)

        for i in range(start, total):
            name, fn = self.stages[i]
            await ctx.checkpoint()
            await ctx.enter_stage(name, progress=i / total)
            outcome = await fn(ctx)
            if isinstance(outcome, Done):
                return outcome

        task = await ctx.snapshot()
        return Done(
            result=task.context.get("result"), result_uri=task.context.get("result_uri")
        )


async def sleep_with_checkpoint(
    ctx: TaskContext, seconds: float, *, step: float = 0.5
) -> None:
    """示例工具：把长等待切成小段，让取消能及时生效。"""
    waited = 0.0
    while waited < seconds:
        await ctx.checkpoint()
        chunk = min(step, seconds - waited)
        await asyncio.sleep(chunk)
        waited += chunk
