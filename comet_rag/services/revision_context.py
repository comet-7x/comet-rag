from __future__ import annotations

import json
from uuid import UUID, uuid4

from comet_rag.engines.chunkers import ChunkDraft
from comet_rag.ports import DocumentResourceLimitExceeded
from comet_rag.ports.document_store import (
    RevisionConflict,
    RevisionDraft,
    RevisionKey,
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
        "draft": {
            "key": _dump_key(draft.key),
            "plan_sha256": draft.plan_sha256,
            "parent_count": draft.parent_count,
            "child_count": draft.child_count,
            "source": draft.source,
            "file_type": draft.file_type,
            "metadata": dict(draft.metadata),
            "task_id": draft.task_id,
        },
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
    raw = _object(fields["draft"], _DRAFT_FIELDS)
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


class RevisionTaskStages:
    """实验性 prepare/ready 阶段；不注册生产 runner，不激活或清理 revision。"""

    def __init__(
        self,
        stager: RevisionStager,
        *,
        max_context_bytes: int = DEFAULT_MAX_REVISION_CONTEXT_BYTES,
    ) -> None:
        if type(max_context_bytes) is not int or max_context_bytes <= 0:
            raise ValueError("max_context_bytes 必须是正整数")
        self._stager = stager
        self._limit = max_context_bytes

    def _load(self, context: dict[str, object], task_id: str) -> IndexHandoff:
        _validate_json_payload(context, name="Revision context", max_bytes=self._limit)
        handoff = load_index_handoff(context["revision_index"], max_bytes=self._limit)
        if (
            handoff.draft.key != _task_key(context["revision_key"])
            or handoff.draft.task_id != task_id
        ):
            raise RevisionConflict(handoff.draft.key, "移交不属于当前任务")
        return handoff

    async def prepare(
        self, ctx: TaskContext, plan: IndexPlan | None = None
    ) -> IndexHandoff:
        """父块成功落库后才持久化移交数据并清掉原文；失败不丢重建输入。"""
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
        payload = dump_index_handoff(handoff, max_bytes=self._limit)
        updates: dict[str, object] = {"revision_index": payload}
        updates.update(dict.fromkeys(_DROP_ON_HANDOFF))
        merged = {**task.context, **updates}
        _validate_json_payload(merged, name="Revision context", max_bytes=self._limit)
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
        ready = await self._stager.resume(
            handoff, embedding_dim=embedding_dim, checkpoint=ctx.checkpoint
        )
        await ctx.report(message=f"已准备 {ready.child_count} 块（待激活）")
        return ready


def _validate_budget(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes 必须是正整数")
