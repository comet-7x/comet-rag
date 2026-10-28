from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from comet_rag.core.logging import logger
from comet_rag.tasks import Task, TaskBusy, TaskContext, TaskStatus, TaskStore

FLAT_WRITE_KIND = "flat-source-write"


def guard_flat_write_deletion(task: Task) -> None:
    if task.kind == FLAT_WRITE_KIND:
        raise TaskBusy("来源写入围栏不可随任务TTL删除")


@dataclass(slots=True)
class FlatWritePermit:
    """只在RPC明确返回后解除不确定性；取消本地协程不能证明远端排干。"""

    uncertain: bool = False

    async def write[T](self, operation: Awaitable[T]) -> T:
        self.uncertain = True
        result = await operation
        self.uncertain = False
        return result


@asynccontextmanager
async def flat_source_write(
    ctx: TaskContext, kb_id: str, source_id: str
) -> AsyncIterator[FlatWritePermit]:
    """同一TaskStore内按来源排他写；异常RPC保留意图，不以租约过期自动接管。

    内部记录不入队，复用唯一幂等键与CAS；同一向量命名空间必须共用TaskStore。
    flat路径仍不是原子换版，失败可留下混合块；需要完整换版保证时使用revision。
    """
    await ctx.checkpoint()
    store: TaskStore = ctx.store
    identity = {"version": 1, "kb_id": kb_id, "source_id": source_id}
    key = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    record = await store.create(
        FLAT_WRITE_KIND,
        request=identity,
        idempotency_key="flat-source-write:" + key,
        context={"writer": None},
    )
    # 该记录从不执行；封存到终态，避免普通PENDING积压统计/补投把它当业务任务。
    if record.status is TaskStatus.PENDING:
        await store.transition(
            record.task_id, TaskStatus.CANCELLED, note="来源围栏不入执行队列"
        )
    token = {"task_id": ctx.task_id, "invocation": uuid4().hex}

    def claim(current: Task) -> None:
        if current.kind != FLAT_WRITE_KIND or current.request != identity:
            raise ValueError("来源写入围栏身份不匹配")
        if "writer" not in current.context or current.context["writer"] is not None:
            raise TaskBusy("该来源已有写入或未排干的RPC，禁止并发覆盖")

    # CAS回包丢失也不授权写入；保留记录由运维核实，不能猜测自己是否拿到许可。
    await store._put_context(record.task_id, {"writer": token}, guard=claim)
    permit = FlatWritePermit()
    try:
        await ctx.checkpoint()
        yield permit
    finally:
        if permit.uncertain:
            logger.error(f"来源写入结果不确定，保留围栏 record={record.task_id}")
        else:

            def release(current: Task) -> None:
                if current.context.get("writer") != token:
                    raise TaskBusy("来源写入许可已变化")

            # 正常/失败收尾都等CAS结束；重复取消不能释放一半后继续关闭Store。
            cleanup = asyncio.create_task(
                store._put_context(record.task_id, {"writer": None}, guard=release)
            )
            cancellation: asyncio.CancelledError | None = None
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancellation = exc
                except Exception:
                    break
            if cancellation is not None:
                if not cleanup.cancelled():
                    cleanup.exception()
                raise cancellation
            cleanup.result()
