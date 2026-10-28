from __future__ import annotations

import asyncio
import json
import math
import re
from hashlib import sha256
from uuid import uuid4

from comet_rag.ports.capture import AsyncEvidencePublisher
from comet_rag.ports.evidence import evidence_digest
from comet_rag.tasks import Task, TaskBusy, TaskStatus, TaskStore

PUBLICATION_PERMISSION_KIND = "evidence-publication-permission"
PERMISSION = "evidence_publication_permission"


def guard_publication_task_deletion(task: Task) -> None:
    if task.kind == PUBLICATION_PERMISSION_KIND or PERMISSION in task.context:
        raise TaskBusy("证据发布许可及不确定探测须保留，不能随任务TTL删除")


async def verify_publication_target(
    store: TaskStore,
    publisher: AsyncEvidencePublisher,
    storage_id: str,
    *,
    timeout_seconds: float = 120,
) -> str:
    """先持久化精确探测身份，再调用原生条件写验收；不确定结果不重放。

    每个SDK实例用自己的探测key。持久化成功记录不能替代新实例的实时验收；
    所有状态都保留给运维核对，不在此删除远端对象或推断旧RPC已排干。
    """
    if not isinstance(storage_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", storage_id
    ):
        raise ValueError("存储别名无效")
    probe = publisher.probe_key
    if not isinstance(probe, str) or not re.fullmatch(
        r"evidence-capability/[0-9a-f]{32}", probe
    ):
        raise ValueError("探测对象身份无效")
    target = evidence_digest(publisher.identity_sha256)
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 120
    ):
        raise ValueError("发布许可deadline须在(0,120]秒内")
    identity = {
        "version": 1,
        "storage_id": storage_id,
        "target_sha256": target,
        "probe_key": probe,
    }
    digest = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    async with asyncio.timeout(timeout_seconds):
        record = await store.create(
            PUBLICATION_PERMISSION_KIND,
            request=identity,
            idempotency_key="evidence-permission:" + digest,
            context={PERMISSION: {"state": "pending"}},
        )
        if record.status is TaskStatus.PENDING:
            await store.transition(
                record.task_id, TaskStatus.CANCELLED, note="发布许可记录不入执行队列"
            )

        def check_identity(current: Task) -> None:
            if (
                current.kind != PUBLICATION_PERMISSION_KIND
                or current.request != identity
            ):
                raise ValueError("发布许可身份不一致")
            if publisher.identity_sha256 != target or publisher.probe_key != probe:
                raise ValueError("运行中的发布目标发生变化")

        current = await store.get(record.task_id)
        if current is None:
            raise ValueError("发布许可记录缺失")
        check_identity(current)
        state = current.context.get(PERMISSION)
        if state == {"state": "verified"}:
            publisher.require_publish_permission()
            return record.task_id
        if state != {"state": "pending"}:
            raise TaskBusy("条件发布探测结果不确定，禁止自动重试同一探测")

        claim = {"state": "probing", "invocation": uuid4().hex}

        def guard_claim(current: Task) -> None:
            check_identity(current)
            if current.context.get(PERMISSION) != {"state": "pending"}:
                raise TaskBusy("条件发布探测已被领取，禁止并发重试")

        await store._put_context(record.task_id, {PERMISSION: claim}, guard=guard_claim)
        await publisher.verify_target()
        publisher.require_publish_permission()

        def guard_verified(current: Task) -> None:
            check_identity(current)
            if current.context.get(PERMISSION) != claim:
                raise TaskBusy("条件发布探测身份已变化")

        await store._put_context(
            record.task_id, {PERMISSION: {"state": "verified"}}, guard=guard_verified
        )
        return record.task_id
