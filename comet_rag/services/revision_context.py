from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from comet_rag.engines.chunkers import ChunkDraft
from comet_rag.ports import DocumentResourceLimitExceeded
from comet_rag.ports.document_store import (
    DocumentStore,
    RevisionConflict,
    RevisionDraft,
    RevisionKey,
    RevisionNotReady,
    RevisionStatus,
    StoredRevision,
)
from comet_rag.services.chunking import (
    _SYSTEM_CHUNK_METADATA_KEYS,
    _merge_chunk_metadata,
    _validate_json_payload,
    dump_chunk_drafts,
    load_chunk_drafts,
    materialize_chunk_drafts,
)
from comet_rag.services.indexing import (
    IndexHandoff,
    IndexPlan,
    PlannedChild,
    RevisionStager,
    _adjacent,
    _snapshot_handoff,
    revision_chunk_id,
)
from comet_rag.services.revision_activation import ActivationOutcome, RevisionActivator
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.tasks import TaskContext
from comet_rag.tasks.runner import TaskContextTooLarge

# 限制整份 Task context 的持久化体积，不是只限制其中的 child 正文。
DEFAULT_MAX_REVISION_CONTEXT_BYTES = 32 * 1024 * 1024
_KEY_FIELDS = {"kb_id", "source_id", "revision_id"}
_DRAFT_FIELDS = {
    "key",
    "plan_sha256",
    "parent_count",
    "child_count",
    "source",
    "file_type",
    "metadata",
    "task_id",
}
_CHILD_FIELDS = {
    "text",
    "ordinal",
    "start_char",
    "end_char",
    "metadata",
    "parent_ordinal",
}
_DROP_ON_HANDOFF = {
    "text",
    "document_blocks",
    "document_metadata",
    "chunk_drafts",
    "chunks",
}


class PublicationPhase(StrEnum):
    """持久化意图/回执；不能作为当前 active 或远端已排干的证明。"""

    ACTIVATING = "activating"
    ACTIVE = "active"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANUP_COMPLETE = "cleanup_complete"


@dataclass(frozen=True, slots=True)
class Publication:
    """绑定已分配 generation 的恢复记录，事实仍须回 Store 复验。"""

    generation: int
    phase: PublicationPhase


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"revision payload 字段必须为 {sorted(fields)}")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("revision payload 字符串字段不得隐式转换")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise TypeError("revision payload 整数字段不得隐式转换")
    return value


def _metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("revision payload metadata 必须是 object")
    return value


def _key(value: object) -> RevisionKey:
    fields = _object(value, _KEY_FIELDS)
    return RevisionKey(
        *(_text(fields[name]) for name in ("kb_id", "source_id", "revision_id"))
    )


def _dump_key(key: RevisionKey) -> dict[str, object]:
    return {
        "kb_id": key.kb_id,
        "source_id": key.source_id,
        "revision_id": key.revision_id,
    }


def _task_key(value: object) -> RevisionKey:
    key = _key(value)
    revision = UUID(key.revision_id)
    if revision.version != 4 or revision.hex != key.revision_id:
        raise ValueError("任务 revision 必须是 UUID4 hex")
    return key


def _dump_draft(draft: RevisionDraft) -> dict[str, object]:
    return {
        "key": _dump_key(draft.key),
        "plan_sha256": draft.plan_sha256,
        "parent_count": draft.parent_count,
        "child_count": draft.child_count,
        "source": draft.source,
        "file_type": draft.file_type,
        "metadata": dict(draft.metadata),
        "task_id": draft.task_id,
    }


def _load_draft(value: object) -> RevisionDraft:
    raw = _object(value, _DRAFT_FIELDS)
    task_id = raw["task_id"]
    draft = RevisionDraft(
        _key(raw["key"]),
        _text(raw["plan_sha256"]),
        _integer(raw["parent_count"]),
        _integer(raw["child_count"]),
        _text(raw["source"]),
        _text(raw["file_type"]),
        _metadata(raw["metadata"]),
        _text(task_id) if task_id is not None else None,
    )
    if draft.parent_count > draft.child_count:
        raise RevisionConflict(draft.key, "准备计数不一致")
    return draft


def load_preparation(context: dict[str, object], task_id: str) -> RevisionDraft | None:
    """外部写入前的计划身份；旧记录可没有，损坏记录不得猜测恢复。"""
    value = context.get("revision_prepare")
    if value is None:
        return None
    fields = _object(value, {"version", "draft"})
    if type(fields["version"]) is not int or fields["version"] != 1:
        raise ValueError("不支持的 preparation version")
    draft = _load_draft(fields["draft"])
    if draft.key != _task_key(context["revision_key"]) or draft.task_id != task_id:
        raise RevisionConflict(draft.key, "准备意图不属于当前任务")
    return draft


def dump_index_handoff(
    handoff: IndexHandoff,
    /,
    *,
    max_bytes: int = DEFAULT_MAX_REVISION_CONTEXT_BYTES,
) -> dict[str, object]:
    """有界 JSON 快照；ID/相邻关系等可推导字段不在 Task 里重复保存。"""
    _validate_budget(max_bytes)
    handoff = _snapshot_handoff(handoff)
    draft = handoff.draft
    parent_ordinals = {
        revision_chunk_id(draft.key, "parent", i): i for i in range(draft.parent_count)
    }
    children = dump_chunk_drafts(
        [
            ChunkDraft(
                c.text,
                c.ordinal,
                c.start_char,
                c.end_char,
                _merge_chunk_metadata(chunk=c.metadata),
            )
            for c in handoff.vector_children
        ]
    )
    for item, child in zip(children, handoff.vector_children, strict=True):
        item["parent_ordinal"] = (
            parent_ordinals[child.parent_id] if child.parent_id else None
        )
    payload: dict[str, object] = {
        "version": 1,
        "draft": _dump_draft(draft),
        "children": children,
    }
    _validate_json_payload(payload, name="IndexHandoff", max_bytes=max_bytes)
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def load_index_handoff(
    payload: object,
    /,
    *,
    max_bytes: int = DEFAULT_MAX_REVISION_CONTEXT_BYTES,
) -> IndexHandoff:
    """只接受版本化窄结构；续跑前仍须从 DocumentStore 复验完整 plan 指纹。"""
    _validate_budget(max_bytes)
    _validate_json_payload(payload, name="IndexHandoff", max_bytes=max_bytes)
    fields = _object(payload, {"version", "draft", "children"})
    if type(fields["version"]) is not int or fields["version"] != 1:
        raise ValueError("不支持的 revision payload version")
    draft = _load_draft(fields["draft"])
    raw_children = fields["children"]
    if (
        not isinstance(raw_children, list)
        or len(raw_children) != draft.child_count
        or draft.parent_count > draft.child_count
    ):
        raise RevisionConflict(draft.key, "移交计数不一致")
    drafts: list[dict[str, object]] = []
    parent_ordinals: list[int | None] = []
    for item in raw_children:
        child = _object(item, _CHILD_FIELDS)
        if _SYSTEM_CHUNK_METADATA_KEYS.intersection(_metadata(child["metadata"])):
            raise ValueError("移交 child metadata 不得携带系统字段")
        ordinal = child["parent_ordinal"]
        if ordinal is not None:
            ordinal = _integer(ordinal)
            if not 0 <= ordinal < draft.parent_count:
                raise RevisionConflict(draft.key, "移交 parent ordinal 越界")
        parent_ordinals.append(ordinal)
        drafts.append({k: v for k, v in child.items() if k != "parent_ordinal"})
    chunk_drafts = load_chunk_drafts(drafts)
    chunks = materialize_chunk_drafts(
        chunk_drafts,
        source_id=draft.key.source_id,
        source=draft.source,
        file_type=draft.file_type,
        kb_id=draft.key.kb_id,
    )
    ids = [revision_chunk_id(draft.key, "child", i) for i in range(len(chunks))]
    children: list[PlannedChild] = []
    for chunk, pure, parent_ordinal in zip(
        chunks, chunk_drafts, parent_ordinals, strict=True
    ):
        previous, following = _adjacent(ids, pure.ordinal)
        parent_id = (
            revision_chunk_id(draft.key, "parent", parent_ordinal)
            if parent_ordinal is not None
            else None
        )
        children.append(
            PlannedChild(
                ids[pure.ordinal],
                pure.ordinal,
                parent_id,
                pure.text,
                pure.start_char,
                pure.end_char,
                previous,
                following,
                {
                    **chunk.metadata,
                    "document_revision": draft.key.revision_id,
                    "parent_id": parent_id,
                    "previous_id": previous,
                    "next_id": following,
                },
            )
        )
    return _snapshot_handoff(IndexHandoff(draft, tuple(children)))


async def get_revision_key(
    ctx: TaskContext, *, kb_id: str, source_id: str
) -> RevisionKey:
    """单一任务拥有者生成一次身份；context 提交后响应丢失也必须复用。"""
    task = await ctx.snapshot()
    if "revision_key" not in task.context:
        key = RevisionKey(kb_id, source_id, uuid4().hex)
        await ctx.put(revision_key=_dump_key(key))
        return key
    key = _task_key(task.context["revision_key"])
    if key.kb_id != kb_id or key.source_id != source_id:
        raise RevisionConflict(key, "任务 revision 来源不一致")
    await ctx.checkpoint()
    return key


def load_task_handoff(
    context: dict[str, object],
    task_id: str,
    *,
    max_bytes: int = DEFAULT_MAX_REVISION_CONTEXT_BYTES,
) -> IndexHandoff:
    """同时验证整份预算与 Task/revision 归属，拒绝跨任务重放。"""
    _validate_json_payload(context, name="Revision context", max_bytes=max_bytes)
    handoff = load_index_handoff(context["revision_index"], max_bytes=max_bytes)
    if (
        handoff.draft.key != _task_key(context["revision_key"])
        or handoff.draft.task_id != task_id
    ):
        raise RevisionConflict(handoff.draft.key, "移交不属于当前任务")
    preparation = load_preparation(context, task_id)
    if preparation is not None and preparation != handoff.draft:
        raise RevisionConflict(handoff.draft.key, "移交与准备意图不一致")
    return handoff


def load_publication(
    context: dict[str, object], draft: RevisionDraft
) -> Publication | None:
    """旧任务可无回执；有记录时必须与同一任务计划严格匹配。"""
    if "revision_publication" not in context:
        return None
    value = _object(
        context["revision_publication"],
        {"version", "key", "plan_sha256", "task_id", "generation", "phase"},
    )
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("不支持的 publication version")
    if (
        _key(value["key"]) != draft.key
        or value["plan_sha256"] != draft.plan_sha256
        or value["task_id"] != draft.task_id
    ):
        raise RevisionConflict(draft.key, "发布记录不属于当前任务计划")
    generation = _integer(value["generation"])
    if generation <= 0:
        raise ValueError("publication generation 必须为正整数")
    return Publication(generation, PublicationPhase(_text(value["phase"])))


def load_recovery_draft(
    context: dict[str, object], task_id: str
) -> RevisionDraft | None:
    """完整移交优先；移交前仅允许用已验证的准备意图做精确回收。"""
    if context.get("revision_index") is not None:
        return load_task_handoff(context, task_id).draft
    _validate_json_payload(
        context, name="Revision context", max_bytes=DEFAULT_MAX_REVISION_CONTEXT_BYTES
    )
    return load_preparation(context, task_id)


class RevisionTaskStages:
    """独立 revision 阶段与有界恢复记录；不注册生产 runner 或 maintenance。

    Task 保存激活意图、结果与清理进度，DocumentStore 决定当前事实。清理完成记录
    保留精确身份供补偿，不能视为远端旧 RPC 已排干的证明。
    """

    def __init__(
        self,
        stager: RevisionStager,
        *,
        document_store: DocumentStore | None = None,
        garbage_collector: RevisionGarbageCollector | None = None,
        max_context_bytes: int = DEFAULT_MAX_REVISION_CONTEXT_BYTES,
    ) -> None:
        if type(max_context_bytes) is not int or max_context_bytes <= 0:
            raise ValueError("max_context_bytes 必须是正整数")
        self._stager = stager
        self._documents = document_store
        self._activator = RevisionActivator(document_store) if document_store else None
        self._gc = garbage_collector
        self._limit = max_context_bytes

    def _load(self, context: dict[str, object], task_id: str) -> IndexHandoff:
        return load_task_handoff(context, task_id, max_bytes=self._limit)

    async def _save_publication(
        self, ctx: TaskContext, draft: RevisionDraft, publication: Publication
    ) -> None:
        try:
            await ctx.put_bounded(
                self._limit,
                revision_publication={
                    "version": 1,
                    "key": _dump_key(draft.key),
                    "plan_sha256": draft.plan_sha256,
                    "task_id": draft.task_id,
                    "generation": publication.generation,
                    "phase": publication.phase.value,
                },
            )
        except TaskContextTooLarge as exc:
            raise DocumentResourceLimitExceeded(str(exc)) from exc

    async def _manifest(
        self, ctx: TaskContext, draft: RevisionDraft, publication: Publication | None
    ) -> StoredRevision:
        if self._documents is None:
            raise ValueError("发布阶段必须显式注入 DocumentStore")
        await ctx.checkpoint()
        manifest = await self._documents.reserve_revision(draft)
        if publication is not None and publication.generation != manifest.generation:
            raise RevisionConflict(draft.key, "发布记录 generation 与 manifest 不一致")
        await ctx.checkpoint()
        return manifest

    async def prepare(
        self, ctx: TaskContext, plan: IndexPlan | None = None
    ) -> IndexHandoff:
        """先记计划身份，再写父块；完整移交提交后才释放重建输入。"""
        task = await ctx.snapshot()
        if "revision_index" in task.context:
            handoff = self._load(task.context, ctx.task_id)
            if (
                plan is not None
                and IndexHandoff.from_plan(plan, task_id=ctx.task_id) != handoff
            ):
                raise RevisionConflict(handoff.draft.key, "任务移交计划发生变化")
            await ctx.checkpoint()
            return handoff
        if plan is None:
            raise ValueError("首次 prepare 必须提供 IndexPlan")
        if plan.key != _task_key(task.context.get("revision_key")):
            raise RevisionConflict(plan.key, "计划不属于当前任务 revision")
        handoff = IndexHandoff.from_plan(plan, task_id=ctx.task_id)
        preparation = load_preparation(task.context, ctx.task_id)
        if preparation is not None and preparation != handoff.draft:
            raise RevisionConflict(plan.key, "任务准备计划发生变化")
        payload = dump_index_handoff(handoff, max_bytes=self._limit)
        updates: dict[str, object] = {
            "revision_index": payload,
            "revision_prepare": None,
        }
        updates.update(dict.fromkeys(_DROP_ON_HANDOFF))
        merged = {**task.context, **updates}
        _validate_json_payload(merged, name="Revision context", max_bytes=self._limit)
        if preparation is None:
            try:
                # 这次 CAS 必须先于 reserve/put_parents；丢响应时重读原意图，不分配新身份。
                await ctx.put_bounded(
                    self._limit,
                    revision_prepare={
                        "version": 1,
                        "draft": _dump_draft(handoff.draft),
                    },
                )
            except TaskContextTooLarge as exc:
                raise DocumentResourceLimitExceeded(str(exc)) from exc
        await self._stager.prepare(plan, task_id=ctx.task_id, checkpoint=ctx.checkpoint)
        try:
            await ctx.put_bounded(self._limit, **updates)
        except TaskContextTooLarge as exc:
            raise DocumentResourceLimitExceeded(str(exc)) from exc
        return handoff

    async def stage(self, ctx: TaskContext, *, embedding_dim: int) -> StoredRevision:
        """保留窄 payload 供失败续跑；激活完成前不能当作任务成功。"""
        task = await ctx.snapshot()
        handoff = self._load(task.context, ctx.task_id)
        if load_publication(task.context, handoff.draft) is not None:
            raise RevisionConflict(
                handoff.draft.key, "已进入发布恢复阶段，禁止重写向量"
            )
        ready = await self._stager.resume(
            handoff, embedding_dim=embedding_dim, checkpoint=ctx.checkpoint
        )
        await ctx.report(message=f"已准备 {ready.child_count} 块（待激活）")
        return ready

    async def activate(self, ctx: TaskContext) -> ActivationOutcome:
        """独立阶段重试同一 manifest；提交成功后报告失败也不能重新准备或回滚。"""
        if self._activator is None:
            raise ValueError("activate 阶段必须显式注入 DocumentStore")
        task = await ctx.snapshot()
        handoff = self._load(task.context, ctx.task_id)
        draft = handoff.draft
        publication = load_publication(task.context, draft)
        manifest = await self._manifest(ctx, draft, publication)
        if publication is not None and publication.phase in (
            PublicationPhase.CLEANUP_PENDING,
            PublicationPhase.CLEANUP_COMPLETE,
        ):
            # 已有 GC 意图的 abandoned/purged 不得再经过激活器或重新 stage。
            if manifest.status not in (
                RevisionStatus.READY,
                RevisionStatus.RETIRED,
                RevisionStatus.ABANDONED,
                RevisionStatus.PURGED,
            ):
                raise RevisionConflict(draft.key, "清理意图与 manifest 状态冲突")
            outcome = ActivationOutcome(manifest, superseded=True)
        else:
            if manifest.status not in (
                RevisionStatus.READY,
                RevisionStatus.ACTIVE,
            ) and not (
                manifest.activated_at is not None
                and manifest.status in (RevisionStatus.RETIRED, RevisionStatus.PURGED)
            ):
                raise RevisionNotReady(draft.key, f"当前状态为 {manifest.status}")
            # 先持久化意图，也提前验证整份 context 预算；提交后写回失败只能恢复，不能回滚。
            await self._save_publication(
                ctx,
                draft,
                Publication(manifest.generation, PublicationPhase.ACTIVATING),
            )
            outcome = await self._activator.activate(draft, checkpoint=ctx.checkpoint)
            await self._save_publication(
                ctx,
                draft,
                Publication(
                    outcome.revision.generation,
                    PublicationPhase.CLEANUP_PENDING
                    if outcome.superseded
                    else PublicationPhase.ACTIVE,
                ),
            )
        await ctx.report(
            message="已被更新版本取代" if outcome.superseded else "文档版本已激活"
        )
        return outcome

    async def cleanup(self, ctx: TaskContext) -> None:
        """独立重试清理意图；complete 也重放，以补偿核验后才落库的旧 RPC。"""
        if self._gc is None:
            raise ValueError("cleanup 阶段必须显式注入 RevisionGarbageCollector")
        task = await ctx.snapshot()
        draft = self._load(task.context, ctx.task_id).draft
        publication = load_publication(task.context, draft)
        if publication is None or publication.phase is PublicationPhase.ACTIVATING:
            raise RevisionConflict(draft.key, "激活结果尚未持久化，不能清理")
        manifest = await self._manifest(ctx, draft, publication)
        if publication.phase is PublicationPhase.ACTIVE:
            if manifest.status is RevisionStatus.ACTIVE:
                return
            if (
                manifest.status not in (RevisionStatus.RETIRED, RevisionStatus.PURGED)
                or manifest.activated_at is None
            ):
                raise RevisionConflict(draft.key, "已激活回执与 manifest 状态冲突")
            # 保存结果后也可能被更新版本取代；先登记意图，再执行不可逆的清理。
            publication = Publication(
                publication.generation, PublicationPhase.CLEANUP_PENDING
            )
            await self._save_publication(ctx, draft, publication)
        await self._gc.collect_inactive(draft.key, checkpoint=ctx.checkpoint)
        await self._save_publication(
            ctx,
            draft,
            Publication(publication.generation, PublicationPhase.CLEANUP_COMPLETE),
        )


def _validate_budget(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes 必须是正整数")
