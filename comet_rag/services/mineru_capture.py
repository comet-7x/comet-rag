from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from hashlib import sha256

from comet_rag.engines.offload import run_sync
from comet_rag.ports.capture import (
    AsyncEvidenceCapture,
    AsyncEvidencePublisher,
    SourceLock,
)
from comet_rag.ports.document_store import RevisionKey
from comet_rag.ports.evidence import EvidenceMissing, evidence_digest
from comet_rag.ports.source import AsyncSourceLoaderPort, LoadedResource, SourceContent
from comet_rag.services.evidence import ParentEvidenceReference, VerifiedEvidence
from comet_rag.services.evidence_publication import verify_publication_target
from comet_rag.tasks import Task, TaskBusy, TaskContext, TaskStore

LOCK = "mineru_capture_lock"
SUBMIT = "mineru_capture_submit"
REMOTE = "mineru_capture_remote"
SOURCE = "mineru_capture_source"
PUBLISH = "mineru_capture_publish"
COMMITTED = "mineru_capture_committed"
MARKERS = frozenset({LOCK, SUBMIT, REMOTE, SOURCE, PUBLISH, COMMITTED})
LEGACY_MARKERS = frozenset(
    {
        "mineru_v1_intent",
        "mineru_v1_observed",
        "mineru_v1_source_verified",
        "mineru_v1_bundle_intent",
        "mineru_v1_bundle_committed",
    }
)


class CaptureUncertain(RuntimeError):
    """已有提交意图但没有可核验的远端ID；禁止自动重复POST。"""


def guard_capture_task_deletion(task: Task) -> None:
    # 旧实验标记同样保留；改名或损坏记录不能绕过保留策略。
    if (MARKERS | LEGACY_MARKERS).intersection(task.context):
        raise TaskBusy("MinerU恢复意图尚未独立归档，禁止删除")


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value
    ):
        raise ValueError("持久化的MinerU身份无效")
    return value


class CaptureJournal:
    """有围栏、只增不改的应用协议；复用Store CAS，不存原生字节或第二份正文。"""

    def __init__(
        self, ctx: TaskContext, identity: dict[str, object], max_bytes: int
    ) -> None:
        if not ctx.has_ownership_fence:
            raise ValueError("MinerU正式采集需要执行尝试围栏")
        self.ctx, self.identity, self.max_bytes = ctx, identity, max_bytes

    async def once(
        self, name: str, value: object, *, prerequisites: dict[str, object]
    ) -> bool:
        created = False

        def guard(task: Task) -> None:
            nonlocal created
            self.ctx._check_write(task)
            if name != LOCK and _json(task.context.get(LOCK)) != _json(self.identity):
                raise ValueError("MinerU来源锁已变化")
            for field, expected in prerequisites.items():
                if field not in task.context or _json(task.context[field]) != _json(
                    expected
                ):
                    raise ValueError("MinerU检查点前置身份不一致")
            if name in task.context and _json(task.context[name]) != _json(value):
                raise ValueError("禁止覆盖MinerU检查点")
            merged = {**task.context, name: value}
            if (
                len(_json({k: v for k, v in merged.items() if k in MARKERS})) > 8192
                or len(_json(merged)) > self.max_bytes
            ):
                raise ValueError("MinerU检查点或Task context超出预算")
            created = name not in task.context

        await self.ctx.store._put_context(self.ctx.task_id, {name: value}, guard=guard)
        return created

    async def establish(self) -> None:
        context = (await self.ctx.snapshot()).context
        if LEGACY_MARKERS.intersection(context):
            raise CaptureUncertain("旧实验采集意图需独立处置，禁止自动转换")
        if MARKERS.intersection(context) and LOCK not in context:
            raise ValueError("MinerU检查点缺少来源锁")
        await self.once(LOCK, self.identity, prerequisites={})

    async def before_submit(self) -> None:
        if not await self.once(
            SUBMIT, {"authorized": True}, prerequisites={LOCK: self.identity}
        ):
            raise CaptureUncertain("MinerU提交结果未知，需人工核对，禁止重复提交")

    async def task_observed(self, task_id: str) -> None:
        await self.once(
            REMOTE,
            {"task_id": _identifier(task_id)},
            prerequisites={LOCK: self.identity, SUBMIT: {"authorized": True}},
        )

    async def source_verified(self, task_id: str, source_file_id: str) -> None:
        await self.once(
            SOURCE,
            {
                "task_id": _identifier(task_id),
                "source_file_id": _identifier(source_file_id),
            },
            prerequisites={LOCK: self.identity, REMOTE: {"task_id": task_id}},
        )


class MinerUWorkflow:
    """来源审批→有围栏采集→发布意图→条件发布/完整回读；默认没有任何注册者。"""

    def __init__(
        self,
        *,
        capture: AsyncEvidenceCapture,
        publisher: AsyncEvidencePublisher,
        storage_id: str,
        source_locks: Sequence[SourceLock],
        max_context_bytes: int = 32 * 1024 * 1024,
        timeout_seconds: float = 900,
    ) -> None:
        # 借用已有引用校验，不让storage_id变成任意端点或对象路径。
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", storage_id):
            raise ValueError("证据存储身份必须是固定别名")
        if type(max_context_bytes) is not int or max_context_bytes <= 0:
            raise ValueError("context预算必须为正整数")
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 900:
            raise ValueError("采集到发布的统一deadline须在(0,900]秒内")
        self._timeout = timeout_seconds
        if not 0 < len(source_locks) <= 1000:
            raise ValueError("来源许可清单须有1..1000条记录")
        locks = {(lock.kb_id, lock.source_id): lock for lock in source_locks}
        if len(locks) != len(source_locks):
            raise ValueError("来源许可清单存在重复身份")
        self._locks = locks
        self._capture, self._publisher = capture, publisher
        self._endpoint_identity, self._storage_identity = (
            capture.endpoint_sha256,
            publisher.identity_sha256,
        )
        self.storage_id, self._limit = storage_id, max_context_bytes
        self.binding_sha256 = sha256(
            _json(
                {
                    "endpoint": evidence_digest(capture.endpoint_sha256),
                    "store": evidence_digest(publisher.identity_sha256),
                    "storage_id": storage_id,
                }
            )
        ).hexdigest()

    async def prepare_publication(self, store: TaskStore) -> str:
        """组合根准入前完成持久化探测；不会提交MinerU任务或写业务证据包。"""
        return await verify_publication_target(store, self._publisher, self.storage_id)

    def source_lock(self, key: RevisionKey) -> SourceLock:
        try:
            return self._locks[key.kb_id, key.source_id]
        except KeyError:
            raise ValueError("该来源未完成受控预检与采集许可") from None

    def journal(self, ctx: TaskContext, key: RevisionKey) -> CaptureJournal:
        return CaptureJournal(
            ctx,
            {
                "version": 1,
                "binding": self.binding_sha256,
                "key": {
                    "kb_id": key.kb_id,
                    "source_id": key.source_id,
                    "revision_id": key.revision_id,
                },
                "source": self.source_lock(key).payload(),
            },
            self._limit,
        )

    def approve_source(self, kb_id: str, source: str) -> SourceLock:
        return self.source_lock(
            RevisionKey(kb_id, SourceContent(source).source_id, "preflight")
        )

    def _validate_state(self, context: dict[str, object], key: RevisionKey) -> None:
        if (
            self._capture.endpoint_sha256 != self._endpoint_identity
            or self._publisher.identity_sha256 != self._storage_identity
        ):
            raise ValueError("运行中的采集/存储绑定发生变化")
        if SUBMIT in context and _json(context[SUBMIT]) != _json({"authorized": True}):
            raise ValueError("MinerU提交许可损坏")
        remote = context.get(REMOTE)
        if REMOTE in context:
            if (
                SUBMIT not in context
                or not isinstance(remote, dict)
                or set(remote) != {"task_id"}
            ):
                raise ValueError("MinerU远端检查点损坏")
            _identifier(remote["task_id"])
        observed = context.get(SOURCE)
        if SOURCE in context:
            if (
                not isinstance(remote, dict)
                or not isinstance(observed, dict)
                or set(observed) != {"task_id", "source_file_id"}
                or observed["task_id"] != remote["task_id"]
            ):
                raise ValueError("MinerU来源核验检查点损坏")
            _identifier(observed["source_file_id"])
        if PUBLISH in context:
            if SOURCE not in context:
                raise ValueError("发布意图缺少来源核验")
            self._reference(context[PUBLISH], key)
        if COMMITTED in context and (
            PUBLISH not in context
            or _json(context[COMMITTED]) != _json(context[PUBLISH])
        ):
            raise ValueError("MinerU发布确认缺少一致的意图")

    async def read_committed(
        self, ctx: TaskContext, key: RevisionKey
    ) -> VerifiedEvidence:
        journal = self.journal(ctx, key)
        await journal.establish()
        context = (await ctx.snapshot()).context
        self._validate_state(context, key)
        reference = self._reference(context.get(COMMITTED), key)
        if context.get(PUBLISH) != reference.payload():
            raise ValueError("持久化证据确认与发布意图不一致")
        return VerifiedEvidence(reference, await self._publisher.read(reference.bundle))

    async def resume_bundle(
        self, ctx: TaskContext, key: RevisionKey
    ) -> VerifiedEvidence | None:
        """先只读恢复已发布包，成功后不要求源临时文件或远端任务仍然存在。"""
        journal = self.journal(ctx, key)
        await journal.establish()
        context = (await ctx.snapshot()).context
        self._validate_state(context, key)
        if COMMITTED in context:
            return await self.read_committed(ctx, key)
        if PUBLISH not in context:
            if SUBMIT in context and REMOTE not in context:
                raise CaptureUncertain("MinerU提交结果未知，禁止自动重提")
            return None
        reference = self._reference(context[PUBLISH], key)
        try:
            verified = VerifiedEvidence(
                reference, await self._publisher.read(reference.bundle)
            )
        except EvidenceMissing:
            return None  # 拒绝/超时/损坏不能伪装成缺对象。
        await journal.once(
            COMMITTED, reference.payload(), prerequisites={PUBLISH: reference.payload()}
        )
        return verified

    async def capture(
        self, ctx: TaskContext, key: RevisionKey, source: LoadedResource
    ) -> VerifiedEvidence:
        async with asyncio.timeout(self._timeout):
            return await self._capture_source(ctx, key, source)

    async def extract(
        self,
        ctx: TaskContext,
        key: RevisionKey,
        loader: AsyncSourceLoaderPort,
        source: SourceContent,
    ) -> VerifiedEvidence:
        """取源、排队、采集与发布共用deadline；已提交的证据不重新下载源文件。"""
        async with asyncio.timeout(self._timeout):
            restored = await self.resume_bundle(ctx, key)
            if restored is not None:
                return restored
            self._publisher.require_publish_permission()
            loaded = await loader.load(source)
            try:
                if (
                    loaded.file_type != "pdf"
                    or loaded.source.source_id != key.source_id
                ):
                    raise ValueError("来源锁只许可同一PDF来源")
                return await self._publish_capture(ctx, key, loaded)
            finally:
                await run_sync(loaded.cleanup)

    def _reference(self, raw: object, key: RevisionKey) -> ParentEvidenceReference:
        reference = ParentEvidenceReference.from_payload(raw)
        lock = self.source_lock(key)
        if (
            reference.storage_id != self.storage_id
            or reference.bundle.key != key
            or reference.bundle.source_sha256 != lock.source_sha256
            or reference.bundle.pages != lock.pages
        ):
            raise ValueError("发布意图与来源锁不一致")
        return reference

    async def _capture_source(
        self, ctx: TaskContext, key: RevisionKey, source: LoadedResource
    ) -> VerifiedEvidence:

        if (
            not isinstance(source, LoadedResource)
            or source.source.source_id != key.source_id
        ):
            raise ValueError("受管来源与revision身份不一致")
        restored = await self.resume_bundle(ctx, key)
        if restored is not None:
            return restored
        return await self._publish_capture(ctx, key, source)

    async def _publish_capture(
        self, ctx: TaskContext, key: RevisionKey, source: LoadedResource
    ) -> VerifiedEvidence:
        journal = self.journal(ctx, key)
        context = (await ctx.snapshot()).context
        intended = None
        if PUBLISH in context:
            intended = self._reference(context[PUBLISH], key)
        remote = context.get(REMOTE)
        task_id = None
        if REMOTE in context:
            if (
                not isinstance(remote, dict)
                or set(remote) != {"task_id"}
                or SUBMIT not in context
            ):
                raise ValueError("MinerU远端检查点损坏")
            task_id = _identifier(remote["task_id"])
        elif SUBMIT in context:
            raise CaptureUncertain("MinerU提交结果未知，禁止自动重提")
        self._publisher.require_publish_permission()
        bundle = await self._capture.capture(
            source.path, self.source_lock(key), key, observer=journal, task_id=task_id
        )
        lock = self.source_lock(key)
        if (
            bundle.reference.key != key
            or bundle.reference.source_sha256 != lock.source_sha256
            or bundle.reference.pages != lock.pages
        ):
            raise ValueError("采集证据不对应已审批来源")
        reference = ParentEvidenceReference(self.storage_id, bundle.reference)
        if intended is not None and intended != reference:
            raise ValueError("恢复产物与原发布意图不同，禁止发布另一份包")
        context = (await ctx.snapshot()).context
        observed_source = context.get(SOURCE)
        if (
            not isinstance(observed_source, dict)
            or set(observed_source) != {"task_id", "source_file_id"}
            or observed_source.get("task_id")
            != (context.get(REMOTE) or {}).get("task_id")
        ):
            raise ValueError("来源未核验，禁止发布")
        _identifier(observed_source["source_file_id"])
        await journal.once(
            PUBLISH,
            reference.payload(),
            prerequisites={LOCK: journal.identity, SOURCE: observed_source},
        )
        await ctx.checkpoint()
        # publish自身完整回读；DB确认只能发生在这次核验之后，响应丢失保留精确意图。
        verified = VerifiedEvidence(reference, await self._publisher.publish(bundle))
        await journal.once(
            COMMITTED, reference.payload(), prerequisites={PUBLISH: reference.payload()}
        )
        return verified
