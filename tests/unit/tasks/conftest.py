"""任务框架测试夹具。

导入路径集中在下方一处。T5 从 poc/task_demo/task/ 迁到 comet_rag/tasks/ 时
确实只改了这一个 import 块，其余测试一行未动 —— 这正是 T4 先于 T5 的价值：
先用测试把行为钉死，搬迁中的任何漂移都会立刻暴露。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

# ── 单一导入源（T5 已从 poc/task_demo/task 迁至此）────────────────────────────────────────────────────
from comet_rag.tasks import (
    Done,
    InMemoryTaskStore,
    InProcessExecutor,
    RetriableError,
    StagePipeline,
    TaskContext,
    TaskService,
    register,
    sleep_with_checkpoint,
)

# ──────────────────────────────────────────────────────────────────────────

# runner 注册表是进程级全局的（register 对重复 kind 会直接抛错），
# 所以 runner 只能在模块导入时注册一次，而每个用例需要的可变状态
# 放这里、由 autouse fixture 清空。
STATE: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    STATE.clear()
    STATE["visited"] = []
    STATE["attempts"] = 0
    yield
    STATE.clear()


@pytest.fixture
def state() -> dict[str, Any]:
    """runner 的执行留痕。用 fixture 而非直接 import，
    tests/ 下没有 __init__.py，跨模块相对导入不成立。"""
    return STATE


@pytest.fixture
def store() -> InMemoryTaskStore:
    return InMemoryTaskStore()


@pytest.fixture
async def executor(store: InMemoryTaskStore) -> AsyncIterator[InProcessExecutor]:
    # retry_backoff 压到 10ms：真实退避是 1s/2s，跑一遍重试就要 3s+，
    # 而 unit 层全套必须 <10s（spec §6）。退避的**语义**由断言覆盖，不靠真等。
    ex = InProcessExecutor(store, max_concurrency=4, retry_backoff=0.01)
    yield ex
    await ex.shutdown(timeout=5.0)


@pytest.fixture
def svc(store: InMemoryTaskStore, executor: InProcessExecutor) -> TaskService:
    return TaskService(store, executor)


# ── 测试用 runner ──────────────────────────────────────────────────────────

three_stage = StagePipeline()


@three_stage.stage("extract")
async def _extract(ctx: TaskContext) -> None:
    STATE["visited"].append("extract")
    await ctx.put(raw="原始内容")


@three_stage.stage("transform")
async def _transform(ctx: TaskContext) -> None:
    STATE["visited"].append("transform")
    task = await ctx.snapshot()
    await ctx.put(items=[task.context["raw"], "已转换"])


@three_stage.stage("load")
async def _load(ctx: TaskContext) -> Done:
    STATE["visited"].append("load")
    task = await ctx.snapshot()
    return Done(result={"count": len(task.context["items"])}, result_uri="s3://out")


register("multi")(three_stage)


failing_flow = StagePipeline()


@failing_flow.stage("s1")
async def _s1(ctx: TaskContext) -> None:
    STATE["visited"].append("s1")


@failing_flow.stage("s2")
async def _s2(ctx: TaskContext) -> None:
    STATE["visited"].append("s2")


@failing_flow.stage("s3")
async def _s3(ctx: TaskContext) -> Done:
    """只在第一次进入时失败 —— 用于验证重试是全量重跑还是断点续跑。"""
    STATE["visited"].append("s3")
    STATE["attempts"] += 1
    if STATE["attempts"] == 1:
        raise RetriableError("上游 503", code="upstream_503")
    return Done(result="ok")


register("resumable")(failing_flow)


@register("slow")
async def _slow(ctx: TaskContext) -> Done:
    await ctx.enter_stage("working")
    # 检查点密一些，取消才能迅速生效，测试不必真等
    await sleep_with_checkpoint(ctx, 5.0, step=0.02)
    return Done(result="不该走到这里")


@register("flaky")
async def _flaky(ctx: TaskContext) -> Done:
    STATE["attempts"] += 1
    if STATE["attempts"] < 3:
        raise RetriableError(
            f"上游 503（第 {STATE['attempts']} 次）", code="upstream_503"
        )
    return Done(result={"ok": True, "tries": STATE["attempts"]})


@register("always_fails")
async def _always_fails(ctx: TaskContext) -> Done:
    STATE["attempts"] += 1
    raise RetriableError("永远失败", code="doomed")


@register("boom")
async def _boom(ctx: TaskContext) -> Done:
    """不可重试的确定性错误（如解析失败），应一次判死不进重试。"""
    raise ValueError("文件损坏")
