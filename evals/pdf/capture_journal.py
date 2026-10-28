from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from comet_rag.infrastructure.sources.base import finish
from comet_rag.ports.document_store import RevisionKey
from comet_rag.tasks import Task, TaskBusy, TaskContext
from evals.pdf._v1_protocol import TERMINAL, digest, identifier
from evals.pdf.evidence_bundle import EvidenceReference, load_bundle

INTENT = "mineru_v1_intent"
OBSERVED = "mineru_v1_observed"
SOURCE_VERIFIED = "mineru_v1_source_verified"
BUNDLE_INTENT = "mineru_v1_bundle_intent"
BUNDLE_COMMITTED = "mineru_v1_bundle_committed"
MARKERS = {INTENT, OBSERVED, SOURCE_VERIFIED, BUNDLE_INTENT, BUNDLE_COMMITTED}
CONTEXT_BYTES = 8 * 1024


def _encoded(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True
    ).encode()


def _intent(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "key",
        "source_sha256",
        "endpoint_sha256",
    }:
        raise ValueError("采集意图损坏")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("采集意图版本不匹配")
    key = value["key"]
    if (
        not isinstance(key, dict)
        or set(key) != {"kb_id", "source_id", "revision_id"}
        or not all(isinstance(v, str) for v in key.values())
    ):
        raise ValueError("采集revision身份损坏")
    RevisionKey(key["kb_id"], key["source_id"], key["revision_id"])
    digest(value["source_sha256"])
    digest(value["endpoint_sha256"])
    return value


def guard_capture_task_deletion(task: Task) -> None:
    """实验保留规则：损坏/半写意图也保留，force、终态和远端404都不是排干证明。"""
    if MARKERS.intersection(task.context):
        raise TaskBusy("MinerU恢复意图尚未独立归档，禁止删除")


class AsyncCaptureJournal:
    """独立Store协议演练，固定小检查点只增不改；没有注册runner或后台任务。"""

    def __init__(self, ctx: TaskContext) -> None:
        if not ctx.has_ownership_fence:
            raise ValueError("采集意图需要执行尝试围栏")
        self.ctx = ctx

    async def _once(
        self, name: str, value: dict[str, object], *, prerequisites: dict[str, object]
    ) -> bool:
        created = False

        def guard(current: Task) -> None:
            nonlocal created
            # 复用TaskContext的取消/尝试围栏，在同一CAS内检查前置与整份预算。
            self.ctx._check_write(current)
            for key, expected in prerequisites.items():
                if key not in current.context or _encoded(
                    current.context[key]
                ) != _encoded(expected):
                    raise ValueError("采集意图前置身份不匹配")
            if name in current.context and _encoded(current.context[name]) != _encoded(
                value
            ):
                raise ValueError("不可覆盖已观察的采集身份")
            if len(_encoded({**current.context, name: value})) > CONTEXT_BYTES:
                raise ValueError("独立采集恢复context超出8KiB")
            created = name not in current.context

        await self.ctx.store._put_context(self.ctx.task_id, {name: value}, guard=guard)
        return created

    async def begin(
        self, *, key: RevisionKey, source_sha256: str, endpoint_sha256: str
    ) -> bool:
        """必须在POST前提交；重读已存在意图返回False，不授权第二次POST。"""
        return await self._once(
            INTENT,
            {
                "schema_version": 1,
                "key": {
                    "kb_id": key.kb_id,
                    "source_id": key.source_id,
                    "revision_id": key.revision_id,
                },
                "source_sha256": digest(source_sha256),
                "endpoint_sha256": digest(endpoint_sha256),
            },
            prerequisites={},
        )

    async def observe(
        self, *, task_id: str, source_file_id: str, intent: dict[str, object]
    ) -> None:
        intent = _intent(intent)
        await self._once(
            OBSERVED,
            {
                "task_id": identifier(task_id),
                "source_file_id": identifier(source_file_id),
            },
            prerequisites={INTENT: intent},
        )

    async def verify_source(self, *, observed: dict[str, object]) -> None:
        """仅在采集器完成远端SHA/长度/名称核验后登记；标记本身不替代HTTP核验。"""
        if set(observed) != {"task_id", "source_file_id"}:
            raise ValueError("已观察任务身份损坏")
        identifier(observed["task_id"])
        identifier(observed["source_file_id"])
        await self._once(
            SOURCE_VERIFIED, {"verified": True}, prerequisites={OBSERVED: observed}
        )

    async def plan_bundle(self, reference: EvidenceReference) -> None:
        task = await self.ctx.snapshot()
        intent = _intent(task.context.get(INTENT))
        if any(
            _encoded(intent.get(k)) != _encoded(v)
            for k, v in reference.payload().items()
            if k in {"key", "source_sha256"}
        ):
            raise ValueError("证据包与采集意图身份不一致")
        await self._once(
            BUNDLE_INTENT,
            reference.payload(),
            prerequisites={INTENT: intent, SOURCE_VERIFIED: {"verified": True}},
        )

    async def commit_local_bundle(
        self, reference: EvidenceReference, output: Path
    ) -> None:
        """独立原型完整回读后确认；文件线程取消时排干，不假设S3客户端已经存在。"""
        await self.ctx.checkpoint()
        await finish(
            asyncio.create_task(asyncio.to_thread(load_bundle, output, reference))
        )
        await self._once(
            BUNDLE_COMMITTED,
            reference.payload(),
            prerequisites={BUNDLE_INTENT: reference.payload()},
        )


@dataclass(frozen=True, slots=True)
class RemoteDisposition:
    next_step: str
    automatic_resubmit: bool = False
    delete_remote_files: bool = False
    remote_drained: bool = False


def remote_disposition(
    *,
    submission: str,
    task_id: str | None,
    source_verified: bool,
    remote_status: str | None,
    bundle_verified: bool,
    not_found: bool = False,
    artifact_invalid: bool = False,
) -> RemoteDisposition:
    """纯处置建议，不发HTTP；404/取消/TTL都不能推出远端字节已物理删除。"""
    if (
        type(source_verified) is not bool
        or type(bundle_verified) is not bool
        or type(not_found) is not bool
        or type(artifact_invalid) is not bool
    ):
        raise ValueError("处置事实必须是bool")
    if task_id is not None:
        identifier(task_id)
    if (
        submission == "not_sent"
        and task_id is None
        and not source_verified
        and remote_status is None
        and not bundle_verified
        and not not_found
    ):
        return RemoteDisposition("fresh_submission_requires_new_intent")
    if task_id is None or not source_verified:
        return RemoteDisposition("manual_reconcile_identity")
    if not_found:
        return RemoteDisposition("manual_reconcile_restart_or_expiry")
    if artifact_invalid:
        return RemoteDisposition("manual_reconcile_invalid_artifact")
    if remote_status in TERMINAL:
        if remote_status == "completed" and not bundle_verified:
            return RemoteDisposition("capture_existing_artifact")
        return RemoteDisposition("retain_receipt_use_upstream_retention")
    if remote_status in {"queued", "running"}:
        return RemoteDisposition("bounded_poll_or_explicit_cancel")
    return RemoteDisposition("manual_reconcile_protocol")
