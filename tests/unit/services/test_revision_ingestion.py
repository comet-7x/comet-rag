from __future__ import annotations

import pytest

from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.engines.pipelines import AsyncPipelineHooks, PipelineConfig
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.ports import (
    DocumentResourceLimitExceeded,
    ExtractedDocument,
    VectorRecord,
)
from comet_rag.ports.document_store import RevisionStatus
from comet_rag.services.ingestion import DocumentExtraction
from comet_rag.services.knowledge_base import KnowledgeBaseService, KnowledgeBaseSpec
from comet_rag.services.revision_context import RevisionTaskStages, load_task_handoff
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.services.revision_ingestion import (
    REVISION_INGEST_KIND,
    RevisionIngestRunner,
)
from comet_rag.services.revision_reconciliation import RevisionReconciler
from comet_rag.tasks import (
    Done,
    Handoff,
    InMemoryTaskStore,
    RetriableError,
    TaskContext,
    TaskStatus,
)
from tests.unit.services.test_ingestion import StubLoader
from tests.unit.services.test_revision_indexing import (
    FakeEmbedding,
    FaultyDocuments,
    FaultyVectors,
    stager,
)


@pytest.fixture
async def environment(tmp_path):
    path = tmp_path / "document.stub"
    path.write_text("abcdef" * 2)
    loader, documents, vectors, model = (
        StubLoader(path),
        FaultyDocuments(),
        FaultyVectors(),
        FakeEmbedding(),
    )
    config = PipelineConfig(chunk_size=3, chunk_overlap=0)
    hooks = AsyncPipelineHooks.fork()

    @hooks.sync_extractor("stub")
    def extract(content, config):
        return ExtractedDocument(content.path.read_text())

    kb = KnowledgeBaseService(
        repository=InMemoryKnowledgeBaseRepository(),
        vector_store=vectors,
        embedding_model="test",
        embedding_dim=2,
    )
    await kb.create(KnowledgeBaseSpec(kb_id="kb"))
    extraction = DocumentExtraction(loader=loader, config=config, hooks=hooks)
    stages = RevisionTaskStages(
        stager(documents, vectors, model),
        document_store=documents,
        garbage_collector=RevisionGarbageCollector(
            document_store=documents, vector_store=vectors
        ),
    )
    runner = RevisionIngestRunner(
        extraction=extraction,
        stages=stages,
        knowledge_base=kb,
        config=config,
        hooks=hooks,
        binding="test-binding",
        kb_ids=["kb"],
        hierarchy=HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(3)),
    )
    store = InMemoryTaskStore()

    async def context(*, lane=None, binding="test-binding", kb_id="kb"):
        task = await store.create(
            REVISION_INGEST_KIND,
            request={"kb_id": kb_id, "source": str(path), "revision_binding": binding},
        )
        await store.transition(
            task.task_id, TaskStatus.RUNNING, worker_id="test", attempts=1
        )
        return TaskContext(store, task.task_id, worker_id="test", attempts=1, lane=lane)

    return runner, stages, context, loader, documents, vectors, model, path


async def test_cpu_handoff_contains_only_durable_children_then_io_publishes(
    environment,
):
    runner, _, context, loader, documents, vectors, model, _ = environment
    ctx = await context(lane="cpu")
    handoff = await runner(ctx)
    assert (
        isinstance(handoff, Handoff)
        and handoff.lane == "io"
        and handoff.resume_stage == "indexing"
    )
    snapshot = await ctx.snapshot()
    draft = load_task_handoff(snapshot.context, ctx.task_id).draft
    assert (
        snapshot.context["text"] is None
        and draft.parent_count == 2
        and draft.child_count == 4
    )
    assert model.calls == 0 and await vectors.count("kb") == 0
    assert [r.stage for r in snapshot.stage_history] == ["extracting", "preparing"]
    await ctx.store.update(ctx.task_id, resume_stage="indexing")
    fresh = TaskContext(ctx.store, ctx.task_id, worker_id="test", attempts=1, lane="io")
    outcome = await runner(fresh)
    assert isinstance(outcome, Done) and outcome.result["superseded"] is False
    assert await documents.get_active_revisions("kb", [draft.key.source_id]) == {
        draft.key.source_id: draft.key.revision_id
    }
    assert loader.loads == 1 and model.calls == 4
    assert runner.lane_of("preparing") == "cpu" and runner.lane_of("cleanup") == "io"


@pytest.mark.parametrize(
    "binding,kb_id", [("other-scope", "kb"), ("test-binding", "outside")]
)
async def test_wrong_namespace_or_kb_fails_before_extraction(
    environment, binding, kb_id
):
    runner, _, context, loader, _, _, model, _ = environment
    with pytest.raises(ValueError):
        await runner(await context(binding=binding, kb_id=kb_id))
    assert loader.loads == model.calls == 0


async def test_parent_ack_loss_reuses_inputs_and_generation(environment):
    runner, _, context, loader, documents, _, model, _ = environment
    ctx = await context()
    documents.fail_parent_on = 1
    with pytest.raises(RetriableError):
        await runner(ctx)
    first = await ctx.snapshot()
    assert first.context["text"] == "abcdef" * 2
    assert first.context["revision_prepare"]["draft"]["parent_count"] == 2
    await ctx.store.update(ctx.task_id, resume_stage="preparing")
    result = await runner(ctx)
    assert isinstance(result, Done)
    draft = load_task_handoff((await ctx.snapshot()).context, ctx.task_id).draft
    assert (await documents.reserve_revision(draft)).generation == 1
    assert loader.loads == 1 and model.calls == 4


@pytest.mark.parametrize("from_scratch", [False, True])
async def test_activation_response_loss_never_reloads_or_embeds(
    environment, monkeypatch, from_scratch
):
    runner, _, context, loader, documents, vectors, model, path = environment
    ctx = await context()
    activate = documents.activate_revision
    injected = False

    async def lost(key):
        nonlocal injected
        result = await activate(key)
        if not injected:
            injected = True
            raise ConnectionError("activation committed, response lost")
        return result

    monkeypatch.setattr(documents, "activate_revision", lost)
    with pytest.raises(RetriableError):
        await runner(ctx)
    draft = load_task_handoff((await ctx.snapshot()).context, ctx.task_id).draft
    committed = await documents.reserve_revision(draft)
    assert committed.status is RevisionStatus.ACTIVE
    calls = model.calls
    if from_scratch:
        path.write_text("changed source must not be loaded")
    else:
        path.unlink()
    await ctx.store.update(
        ctx.task_id, resume_stage=None if from_scratch else "activating"
    )
    result = await runner(ctx)
    assert (
        isinstance(result, Done)
        and result.result["revision_id"] == draft.key.revision_id
    )
    after = await documents.reserve_revision(draft)
    assert (
        after.generation == committed.generation
        and after.activated_at == committed.activated_at
    )
    assert loader.loads == 1 and model.calls == calls and await vectors.count("kb") == 4


async def test_superseded_and_cleanup_failure_resume_without_republishing(
    environment, monkeypatch
):
    runner, _, context, loader, documents, vectors, model, _ = environment
    old = await context(lane="cpu")
    await runner(old)
    await old.store.update(old.task_id, resume_stage="indexing")
    ready = documents.mark_ready
    entered = False

    async def replace_before_old_activation(key):
        nonlocal entered
        value = await ready(key)
        if not entered:
            entered = True
            newer = await context()
            result = await runner(newer)
            assert isinstance(result, Done) and not result.result["superseded"]
        return value

    monkeypatch.setattr(documents, "mark_ready", replace_before_old_activation)
    old.lane = "io"
    delete = vectors.delete
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        await delete(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise ConnectionError("delete committed without acknowledgment")

    monkeypatch.setattr(vectors, "delete", interrupted)
    with pytest.raises(RetriableError):
        await runner(old)
    snapshot = await old.snapshot()
    assert snapshot.context["revision_publication"]["phase"] == "cleanup_pending"
    embeds = model.calls
    await old.store.update(old.task_id, resume_stage="cleanup")
    result = await runner(old)
    assert isinstance(result, Done) and result.result["superseded"] is True
    assert model.calls == embeds and loader.loads == 2
    assert await vectors.count("kb") == 4
    draft = load_task_handoff(snapshot.context, old.task_id).draft
    assert (await documents.reserve_revision(draft)).status is RevisionStatus.PURGED


async def test_new_active_cleanup_retries_its_persisted_previous_version(
    environment, monkeypatch
):
    runner, _, context, _, documents, vectors, model, _ = environment
    old = await context()
    await runner(old)
    old_draft = load_task_handoff((await old.snapshot()).context, old.task_id).draft
    new = await context()
    delete = vectors.delete
    failure = True

    async def lost(*args, **kwargs):
        nonlocal failure
        await delete(*args, **kwargs)
        if failure:
            failure = False
            raise ConnectionError("old revision delete response lost")

    monkeypatch.setattr(vectors, "delete", lost)
    with pytest.raises(RetriableError):
        await runner(new)
    new_draft = load_task_handoff((await new.snapshot()).context, new.task_id).draft
    assert await documents.get_active_revisions("kb", [new_draft.key.source_id]) == {
        new_draft.key.source_id: new_draft.key.revision_id
    }
    embeds = model.calls
    await new.store.update(new.task_id, resume_stage="cleanup")
    await runner(new)
    assert (
        model.calls == embeds
        and (await documents.reserve_revision(old_draft)).status
        is RevisionStatus.PURGED
    )
    assert await vectors.count("kb") == 4
    # 回执不证明旧 RPC 排干：旧源任务仍可被独立持续对账发现并精确补偿。
    await old.store.transition(old.task_id, TaskStatus.SUCCEEDED)
    await vectors.upsert(
        "kb",
        [
            VectorRecord(
                "late",
                "late",
                [1.0, 0.0],
                {
                    "source_id": old_draft.key.source_id,
                    "document_revision": old_draft.key.revision_id,
                },
            )
        ],
    )
    reconciler = RevisionReconciler(
        task_store=old.store,
        document_store=documents,
        garbage_collector=RevisionGarbageCollector(
            document_store=documents, vector_store=vectors
        ),
        task_kind=REVISION_INGEST_KIND,
    )
    await reconciler.reconcile(await old.snapshot())
    assert await vectors.count("kb") == 4


async def test_extraction_budget_rejects_before_persisting_large_input(environment):
    runner, _, context, _, _, _, model, _ = environment
    runner._extraction._max_chunk_context_bytes = 10
    ctx = await context()
    with pytest.raises(DocumentResourceLimitExceeded):
        await runner(ctx)
    assert (await ctx.snapshot()).context == {} and model.calls == 0


async def test_revision_key_cannot_push_the_retained_context_over_budget(environment):
    from comet_rag.services.revision_context import get_revision_key
    from comet_rag.tasks.runner import TaskContextTooLarge

    _, _, context, _, _, _, _, _ = environment
    ctx = await context()
    await ctx.put(text="near limit")
    before = await ctx.snapshot()
    with pytest.raises(TaskContextTooLarge):
        await get_revision_key(ctx, kb_id="kb", source_id="source", max_bytes=30)
    assert (await ctx.snapshot()).context == before.context


async def test_recovery_payload_cannot_cross_request_knowledge_base(environment):
    runner, _, context, loader, _, _, model, _ = environment
    ctx = await context(lane="cpu")
    await runner(ctx)
    task = await ctx.snapshot()
    await runner._kb.create(KnowledgeBaseSpec(kb_id="other"))
    runner._kb_ids = frozenset({"kb", "other"})
    await ctx.store.update(
        ctx.task_id, request={**task.request, "kb_id": "other"}, resume_stage="indexing"
    )
    ctx.lane = "io"
    with pytest.raises(ValueError, match="请求知识库"):
        await runner(ctx)
    assert loader.loads == 1 and model.calls == 0


async def test_recovery_and_writer_use_the_same_explicit_context_budget(environment):
    from comet_rag.ports import DocumentResourceLimitExceeded
    from comet_rag.services.revision_context import load_recovery_draft

    runner, _, context, _, _, _, _, _ = environment
    ctx = await context(lane="cpu")
    await runner(ctx)
    task = await ctx.snapshot()
    with pytest.raises(DocumentResourceLimitExceeded):
        load_recovery_draft(task.context, ctx.task_id, max_bytes=10)
    assert load_recovery_draft(task.context, ctx.task_id, max_bytes=100_000) is not None
    runner._limit = 10
    ctx.lane = "io"
    with pytest.raises(DocumentResourceLimitExceeded):
        await runner(ctx)
