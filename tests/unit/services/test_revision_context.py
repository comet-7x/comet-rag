from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest

from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.ports import DocumentResourceLimitExceeded, NormalizedDocument
from comet_rag.ports.document_store import (
    RevisionConflict,
    RevisionKey,
    RevisionNotReady,
    RevisionStatus,
    StoredParent,
)
from comet_rag.services.indexing import IndexHandoff, IndexPlanner
from comet_rag.services.revision_context import (
    RevisionTaskStages,
    dump_index_handoff,
    get_revision_key,
    load_index_handoff,
)
from comet_rag.tasks import InMemoryTaskStore, TaskContext, TaskStatus
from comet_rag.tasks.runner import TaskCancelled
from comet_rag.tasks.store import TaskLeaseLost
from tests.unit.services.test_revision_indexing import (
    FakeEmbedding,
    FaultyDocuments,
    FaultyVectors,
    make_plan,
    stager,
)


def _wire(handoff: IndexHandoff) -> dict[str, Any]:
    # 故障测试要向任意深度的 JSON 字段注入错误类型，不能用合法 wire 类型限制它。
    return cast("dict[str, Any]", dump_index_handoff(handoff))


@pytest.mark.parametrize("hierarchy", [True, False])
def test_wire_roundtrip_is_narrow_and_reconstructs_deterministic_system_fields(
    hierarchy: bool,
) -> None:
    original = IndexHandoff.from_plan(make_plan(hierarchy=hierarchy), task_id="task")
    payload = _wire(original)
    assert load_index_handoff(json.loads(json.dumps(payload))) == original
    assert set(payload) == {"version", "draft", "children"}
    children = payload["children"]
    assert isinstance(children, list)
    assert all(
        set(child)
        == {"text", "ordinal", "start_char", "end_char", "metadata", "parent_ordinal"}
        for child in children
    )
    assert [c["parent_ordinal"] for c in children] == (
        [0, 0, 1, 1] if hierarchy else [None] * 4
    )
    assert all(
        not {
            "parent_id",
            "document_revision",
            "source_id",
            "previous_id",
            "next_id",
        }.intersection(c["metadata"])
        for c in children
    )


def test_wire_metadata_is_deeply_isolated_and_utf8_budget_is_exact() -> None:
    chunks = FixedSizeChunker(3).split("中文测试")
    plan = IndexPlanner().plan(
        chunks,
        key=RevisionKey("kb", "source", "r1"),
        source="中文.md",
        file_type="md",
        request_metadata={"nested": {"a": [1]}},
    )
    handoff = IndexHandoff.from_plan(plan)
    payload = _wire(handoff)
    size = len(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    )
    assert dump_index_handoff(handoff, max_bytes=size) == payload
    with pytest.raises(DocumentResourceLimitExceeded):
        dump_index_handoff(handoff, max_bytes=size - 1)
    restored = load_index_handoff(payload, max_bytes=size)
    payload["children"][0]["metadata"]["nested"]["a"].append(2)
    assert restored.vector_children[0].metadata["nested"] == {"a": [1]}
    assert handoff.vector_children[0].metadata["nested"] == {"a": [1]}


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "extra-parent",
        "extra-key",
        "count",
        "huge-parents",
        "bool-ordinal",
        "missing-parent",
        "system-field",
        "nan",
    ],
)
def test_wire_rejects_malformed_fields_counts_and_relationships(fault: str) -> None:
    payload = _wire(IndexHandoff.from_plan(make_plan()))
    if fault == "version":
        payload["version"] = True
    elif fault == "extra-parent":
        payload["parents"] = []
    elif fault == "extra-key":
        payload["draft"]["key"]["tenant"] = "other"
    elif fault == "count":
        payload["draft"]["child_count"] = True
    elif fault == "huge-parents":
        payload["draft"]["parent_count"] = 10**12
    elif fault == "bool-ordinal":
        payload["children"][0]["parent_ordinal"] = True
    elif fault == "missing-parent":
        payload["children"][0]["parent_ordinal"] = None
    elif fault == "system-field":
        payload["children"][0]["metadata"]["source_id"] = "forged"
    else:
        payload["children"][0]["metadata"]["bad"] = float("nan")
    with pytest.raises((TypeError, ValueError, RevisionConflict)):
        load_index_handoff(payload)


class ReadFaultDocuments(FaultyDocuments):
    fault: str | None = None
    read_sizes: list[int]

    def __init__(self) -> None:
        super().__init__()
        self.read_sizes = []

    async def get_parents(
        self, kb_id: str, parent_ids: Sequence[str]
    ) -> dict[str, StoredParent]:
        self.read_sizes.append(len(parent_ids))
        parents = await super().get_parents(kb_id, parent_ids)
        if self.fault == "missing":
            return {}
        if parents and self.fault:
            parent_id = next(iter(parents))
            p = parents[parent_id]
            parents[parent_id] = (
                replace(p, text="broken")
                if self.fault == "digest"
                else replace(p, key=RevisionKey("other-kb", "source", "r1"))
            )
        return parents


@pytest.mark.parametrize("fault", ["missing", "digest", "identity", "child-digest"])
async def test_resume_rejects_missing_or_corrupt_plan_before_embedding_and_vectors(
    fault: str,
) -> None:
    documents, vectors, model = ReadFaultDocuments(), FaultyVectors(), FakeEmbedding()
    writer = stager(documents, vectors, model)
    handoff = await writer.prepare(make_plan())
    if fault == "child-digest":
        handoff = replace(
            handoff,
            vector_children=(
                replace(handoff.vector_children[0], text="zzz"),
                *handoff.vector_children[1:],
            ),
        )
    else:
        documents.fault = fault
    with pytest.raises((RevisionConflict, RevisionNotReady)):
        await writer.resume(handoff, embedding_dim=2)
    assert model.calls == vectors.calls == 0


async def _context(store: InMemoryTaskStore) -> TaskContext:
    task = await store.create("revision-test")
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="cpu", attempts=1
    )
    ctx = TaskContext(store, task.task_id, worker_id="cpu", attempts=1)
    await ctx.put(
        text="abcdEFGHijkl", document_blocks=[], document_metadata={"owner": "doc"}
    )
    return ctx


def _task_plan(key: RevisionKey):
    hierarchy = HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(3)).split(
        NormalizedDocument(markdown="abcdEFGHijkl")
    )
    return IndexPlanner().plan(hierarchy, key=key, source="sample.md", file_type="md")


async def test_task_identity_reused_by_new_context_and_source_mismatch_is_rejected() -> (
    None
):
    ctx = await _context(InMemoryTaskStore())
    first = await get_revision_key(ctx, kb_id="kb", source_id="source")
    again = await get_revision_key(
        TaskContext(ctx.store, ctx.task_id), kb_id="kb", source_id="source"
    )
    assert (
        first == again
        and UUID(first.revision_id).version == 4
        and len(first.revision_id) == 32
    )
    with pytest.raises(RevisionConflict):
        await get_revision_key(ctx, kb_id="other-kb", source_id="source")


@pytest.mark.parametrize("failure", ["parents", "context-before", "context-after"])
async def test_cpu_failure_preserves_rebuild_input_or_replays_persisted_handoff(
    failure: str, monkeypatch
) -> None:
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    documents, vectors, model = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    stages = RevisionTaskStages(stager(documents, vectors, model))
    original = ctx.put_bounded
    if failure == "parents":
        documents.fail_parent_on = 1
    else:

        async def lose_response(limit, **values):
            if "revision_index" not in values:
                return await original(limit, **values)
            if failure == "context-after":
                await original(limit, **values)
            raise ConnectionError("context response lost")

        monkeypatch.setattr(ctx, "put_bounded", lose_response)
    with pytest.raises(ConnectionError):
        await stages.prepare(ctx, _task_plan(key))
    saved = await ctx.snapshot()
    assert model.calls == vectors.calls == 0
    assert (saved.context["text"] is None) == (failure == "context-after")
    if failure != "parents":
        monkeypatch.setattr(ctx, "put_bounded", original)
    documents.fail_parent_on = 0
    replay = await stages.prepare(
        ctx, None if failure == "context-after" else _task_plan(key)
    )
    assert replay.draft.key == key
    assert (await ctx.snapshot()).context["text"] is None


async def test_new_io_context_replays_partial_vectors_and_ready_response_loss() -> None:
    store = InMemoryTaskStore()
    cpu = await _context(store)
    key = await get_revision_key(cpu, kb_id="kb", source_id="source")
    documents, vectors, model = ReadFaultDocuments(), FaultyVectors(), FakeEmbedding()
    await RevisionTaskStages(stager(documents, vectors, model)).prepare(
        cpu, _task_plan(key)
    )
    await store.transition(
        cpu.task_id, TaskStatus.PENDING, worker_id=None, resume_stage="indexing"
    )
    await store.transition(cpu.task_id, TaskStatus.RUNNING, worker_id="io", attempts=2)
    io = TaskContext(store, cpu.task_id, worker_id="io", attempts=2)
    vectors.fail_on = 1
    with pytest.raises(ConnectionError):
        await RevisionTaskStages(stager(documents, vectors, model)).stage(
            io, embedding_dim=2
        )
    assert await documents.get_active_revisions("kb", ["source"]) == {"source": None}
    vectors.fail_on = 0
    documents.fail_ready = True
    with pytest.raises(ConnectionError):
        await RevisionTaskStages(stager(documents, vectors, model)).stage(
            io, embedding_dim=2
        )
    calls = model.calls
    documents.fail_ready = False
    ready = await RevisionTaskStages(stager(documents, vectors, model)).stage(
        io, embedding_dim=2
    )
    assert ready.status is RevisionStatus.READY and ready.generation == 1
    assert (
        model.calls == calls and documents.read_sizes and max(documents.read_sizes) == 1
    )
    assert (
        await vectors.count(
            "kb", filter={"source_id": "source", "document_revision": key.revision_id}
        )
        == 4
    )
    assert (await io.snapshot()).context["revision_index"]
    assert (await io.snapshot()).status is TaskStatus.RUNNING
    with pytest.raises(TaskLeaseLost):
        await RevisionTaskStages(stager(documents, vectors, model)).stage(
            cpu, embedding_dim=2
        )


async def test_context_budget_rejects_before_reserve_even_when_child_payload_fits() -> (
    None
):
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    await ctx.put(unrelated="x" * 5000)
    documents, vectors, model = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    stages = RevisionTaskStages(
        stager(documents, vectors, model), max_context_bytes=4000
    )
    with pytest.raises(DocumentResourceLimitExceeded):
        await stages.prepare(ctx, _task_plan(key))
    assert documents.parent_calls == vectors.calls == model.calls == 0
    assert (await ctx.snapshot()).context["text"] == "abcdEFGHijkl"
    probe = IndexHandoff.from_plan(make_plan("probe")).draft
    assert (await documents.reserve_revision(probe)).generation == 1


@pytest.mark.parametrize("fault", ["cancel", "lease"])
async def test_post_embedding_fence_prevents_late_vector_write(fault: str) -> None:
    store = InMemoryTaskStore()
    ctx = await _context(store)
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")

    class LosingEmbedding(FakeEmbedding):
        changed = False

        async def _embed(self, data, **kwargs) -> list[float]:
            result = await super()._embed(data, **kwargs)
            if not self.changed:
                self.changed = True
                await store.transition(
                    ctx.task_id,
                    TaskStatus.CANCELLING if fault == "cancel" else TaskStatus.PENDING,
                )
            return result

    documents, vectors, model = FaultyDocuments(), FaultyVectors(), LosingEmbedding()
    stages = RevisionTaskStages(stager(documents, vectors, model))
    await stages.prepare(ctx, _task_plan(key))
    with pytest.raises(TaskCancelled if fault == "cancel" else TaskLeaseLost):
        await stages.stage(ctx, embedding_dim=2)
    assert model.calls > 0 and vectors.calls == 0


async def test_late_context_growth_is_rejected_inside_cas_after_parent_io(
    monkeypatch,
) -> None:
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    documents, vectors, model = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    put = documents.put_parents

    async def grow_context(revision, parents):
        await put(revision, parents)
        await ctx.put(concurrent="x" * 5000)

    monkeypatch.setattr(documents, "put_parents", grow_context)
    stages = RevisionTaskStages(
        stager(documents, vectors, model), max_context_bytes=4000
    )
    with pytest.raises(DocumentResourceLimitExceeded):
        await stages.prepare(ctx, _task_plan(key))
    assert (await ctx.snapshot()).context["text"] == "abcdEFGHijkl"
    assert documents.parent_calls == 2 and vectors.calls == model.calls == 0
    assert await documents.get_active_revisions("kb", ["source"]) == {"source": None}


async def test_revision_identity_commit_response_loss_reuses_same_key(
    monkeypatch,
) -> None:
    ctx = await _context(InMemoryTaskStore())
    put = ctx.put

    async def lose_response(**values):
        await put(**values)
        raise ConnectionError("identity response lost")

    monkeypatch.setattr(ctx, "put", lose_response)
    with pytest.raises(ConnectionError):
        await get_revision_key(ctx, kb_id="kb", source_id="source")
    stored = (await ctx.snapshot()).context["revision_key"]
    resumed = await get_revision_key(
        TaskContext(ctx.store, ctx.task_id), kb_id="kb", source_id="source"
    )
    assert resumed.revision_id == stored["revision_id"]


@pytest.mark.parametrize("fault", ["task", "key"])
async def test_context_cannot_replay_another_tasks_handoff(fault: str) -> None:
    ctx = await _context(InMemoryTaskStore())
    key = await get_revision_key(ctx, kb_id="kb", source_id="source")
    documents, vectors, model = FaultyDocuments(), FaultyVectors(), FakeEmbedding()
    stages = RevisionTaskStages(stager(documents, vectors, model))
    await stages.prepare(ctx, _task_plan(key))
    snapshot = await ctx.snapshot()
    if fault == "task":
        snapshot.context["revision_index"]["draft"]["task_id"] = "another-task"
    else:
        snapshot.context["revision_key"]["source_id"] = "another-source"
    await ctx.put(**snapshot.context)
    with pytest.raises(RevisionConflict):
        await stages.stage(ctx, embedding_dim=2)
    assert vectors.calls == model.calls == 0
