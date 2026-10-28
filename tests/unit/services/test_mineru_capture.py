from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.evidence.s3 import AsyncS3EvidenceStore, S3EvidenceError
from comet_rag.infrastructure.evidence.source_locks import load_source_locks
from comet_rag.infrastructure.extractors.mineru_capture import (
    AsyncMinerUEvidenceCapture,
)
from comet_rag.infrastructure.extractors.mineru_v1 import (
    AsyncMinerUV1Collector,
    CaptureError,
    CaptureSource,
)
from comet_rag.ports.capture import SourceLock
from comet_rag.ports.document_store import RevisionKey
from comet_rag.ports.source import LoadedResource, SourceContent
from comet_rag.services.mineru_capture import (
    COMMITTED,
    LOCK,
    MARKERS,
    PUBLISH,
    REMOTE,
    SOURCE,
    SUBMIT,
    CaptureUncertain,
    MinerUWorkflow,
    guard_capture_task_deletion,
)
from comet_rag.tasks import InMemoryTaskStore, TaskBusy, TaskContext, TaskStatus
from comet_rag.tasks.store import TaskLeaseLost
from tests.unit.test_mineru_v1_capture import Server, limits
from tests.unit.test_pdf_s3_evidence import Client


@pytest.fixture
async def capture_environment(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nlocked unit fixture")
    origin = SourceContent(str(source))
    lock = SourceLock(
        "kb",
        origin.source_id,
        sha256(source.read_bytes()).hexdigest(),
        source.stat().st_size,
        1,
        "sample.pdf",
        "review-20261028",
    )
    key = RevisionKey("kb", origin.source_id, "revision")
    server = Server(
        CaptureSource(source, lock.filename, lock.source_sha256, lock.size, lock.pages)
    )
    client = Client()
    client.meta.endpoint_url = "http://storage.test"
    publisher = AsyncS3EvidenceStore(
        client=client,
        bucket="isolated",
        gate=Gate(name="s3", limit=1, max_waiting=2, acquire_timeout=1),
    )
    await publisher.verify_target()
    tasks = InMemoryTaskStore()
    task = await tasks.create("capture-test")
    await tasks.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="worker", attempts=1
    )
    ctx = TaskContext(tasks, task.task_id, worker_id="worker", attempts=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), trust_env=False
    ) as http:
        collector = AsyncMinerUV1Collector(
            "http://localhost",
            client=http,
            gate=Gate(name="mineru", limit=1, max_waiting=2, acquire_timeout=1),
            limits=limits(),
        )
        adapter = AsyncMinerUEvidenceCapture(collector, temporary_directory=tmp_path)
        workflow = MinerUWorkflow(
            capture=adapter,
            publisher=publisher,
            storage_id="documents_v1",
            source_locks=[lock],
        )
        yield SimpleNamespace(
            workflow=workflow,
            ctx=ctx,
            key=key,
            source=LoadedResource(source, origin, metadata={"file_type": "pdf"}),
            lock=lock,
            server=server,
            client=client,
            publisher=publisher,
            collector=collector,
            adapter=adapter,
        )


async def test_capture_persists_identity_and_publication_then_reuses_verified_bundle(
    capture_environment, tmp_path
):
    env = capture_environment
    verified = await env.workflow.capture(env.ctx, env.key, env.source)
    assert env.server.posts == 1
    assert verified.projection.document.markdown == "Source text"
    context = (await env.ctx.snapshot()).context
    assert set(context) == MARKERS
    assert context[PUBLISH] == context[COMMITTED] == verified.reference.payload()
    assert context[LOCK]["source"] == env.lock.payload()
    assert len(json.dumps(context).encode()) < 8192
    assert str(env.source.path) not in json.dumps(context)
    calls, writes = list(env.server.calls), list(env.client.writes)
    again = await env.workflow.capture(env.ctx, env.key, env.source)
    assert again.reference == verified.reference
    assert env.server.calls == calls and env.client.writes == writes
    assert not list(tmp_path.glob("comet-mineru-*"))


@pytest.mark.parametrize(
    "fault", ["unapproved", "changed_bytes", "old_attempt", "no_publish_permit"]
)
async def test_capture_rejects_before_post_or_publish(capture_environment, fault):
    env = capture_environment
    key = env.key
    error = (ValueError, CaptureError)
    if fault == "unapproved":
        key = replace(key, kb_id="other")
    elif fault == "changed_bytes":
        env.source.path.write_bytes(b"X" * env.lock.size)
    elif fault == "old_attempt":
        await env.ctx.store.update(env.ctx.task_id, attempts=2)
        error = TaskLeaseLost
    else:
        env.publisher._verified = False
        error = S3EvidenceError
    before = list(env.client.writes)
    with pytest.raises(error):
        await env.workflow.capture(env.ctx, key, env.source)
    assert env.server.posts == 0 and env.client.writes == before
    assert SUBMIT not in (await env.ctx.snapshot()).context


@pytest.mark.parametrize("where", [SUBMIT, REMOTE, SOURCE, PUBLISH, COMMITTED])
async def test_checkpoint_committed_but_response_lost_recovers_without_double_post(
    capture_environment, monkeypatch, where
):
    env = capture_environment
    original = env.ctx.store._put_context
    lost = False

    async def lose_response(task_id, values, **kwargs):
        nonlocal lost
        result = await original(task_id, values, **kwargs)
        if where in values and not lost:
            lost = True
            raise OSError("injected checkpoint commit reply loss")
        return result

    monkeypatch.setattr(env.ctx.store, "_put_context", lose_response)
    with pytest.raises((CaptureError, OSError)):
        await env.workflow.capture(env.ctx, env.key, env.source)
    context = (await env.ctx.snapshot()).context
    assert where in context
    if where == SUBMIT:
        assert env.server.posts == 0
        with pytest.raises(CaptureUncertain):
            await env.workflow.capture(env.ctx, env.key, env.source)
        assert env.server.posts == 0
    else:
        if where == PUBLISH:
            assert not any(k.startswith("evidence/v1/") for k in env.client.writes)
        fresh = TaskContext(
            env.ctx.store, env.ctx.task_id, worker_id="worker", attempts=1
        )
        verified = await env.workflow.capture(fresh, env.key, env.source)
        assert verified.projection.document.markdown == "Source text"
        assert env.server.posts == 1


async def test_post_response_loss_retains_unknown_submission_and_never_reposts(
    capture_environment,
):
    env = capture_environment

    def lose(request):
        if request.method == "POST":
            raise httpx.ReadError("response lost")
        return None

    env.server.special = lose
    with pytest.raises(CaptureError, match="submission_unknown"):
        await env.workflow.capture(env.ctx, env.key, env.source)
    assert SUBMIT in (await env.ctx.snapshot()).context
    assert REMOTE not in (await env.ctx.snapshot()).context
    with pytest.raises(CaptureUncertain):
        await env.workflow.capture(env.ctx, env.key, env.source)
    assert env.server.posts == 1 and env.server.deletes == []


async def test_manifest_reply_loss_is_read_only_recovery_even_if_write_permit_revoked(
    capture_environment,
):
    env = capture_environment
    env.client.lose_manifest = True
    with pytest.raises(S3EvidenceError):
        await env.workflow.capture(env.ctx, env.key, env.source)
    assert PUBLISH in (await env.ctx.snapshot()).context
    assert COMMITTED not in (await env.ctx.snapshot()).context
    writes, calls = list(env.client.writes), list(env.server.calls)
    env.publisher._verified = False
    verified = await env.workflow.capture(env.ctx, env.key, env.source)
    assert verified.projection.document.markdown == "Source text"
    assert env.client.writes == writes and env.server.calls == calls


async def test_incomplete_publication_cannot_be_replaced_by_changed_remote_artifact(
    capture_environment, monkeypatch
):
    env = capture_environment
    original = env.publisher.publish

    async def fail(bundle):
        raise OSError("publication unavailable")

    monkeypatch.setattr(env.publisher, "publish", fail)
    with pytest.raises(OSError):
        await env.workflow.capture(env.ctx, env.key, env.source)
    intended = (await env.ctx.snapshot()).context[PUBLISH]
    env.server.data = env.server.data.replace(b"Source text", b"Changed text")
    monkeypatch.setattr(env.publisher, "publish", original)
    writes = list(env.client.writes)
    with pytest.raises(ValueError, match="原发布意图"):
        await env.workflow.capture(env.ctx, env.key, env.source)
    assert env.server.posts == 1 and env.client.writes == writes
    assert (await env.ctx.snapshot()).context[PUBLISH] == intended


@pytest.mark.parametrize("change", ["endpoint", "storage_id", "approval"])
async def test_resume_rejects_changed_binding_or_source_approval(
    capture_environment, change
):
    env = capture_environment
    await env.workflow.journal(env.ctx, env.key).establish()
    storage_id, lock = "documents_v1", env.lock
    if change == "endpoint":
        env.collector.base_url = "http://other-endpoint.test"
    elif change == "storage_id":
        storage_id = "other_store"
    else:
        lock = replace(lock, approval_id="different-approval")
    changed = MinerUWorkflow(
        capture=env.adapter,
        publisher=env.publisher,
        storage_id=storage_id,
        source_locks=[lock],
    )
    with pytest.raises(ValueError, match="覆盖"):
        await changed.capture(env.ctx, env.key, env.source)
    assert env.server.posts == 0


async def test_publication_cancellation_preserves_exact_intent_and_releases_gates(
    capture_environment, monkeypatch
):
    env = capture_environment
    original = env.client.put_object

    async def block_manifest(**kwargs):
        if kwargs["Key"].endswith("/manifest.json"):
            env.client.hang_put = kwargs["Key"]
        return await original(**kwargs)

    monkeypatch.setattr(env.client, "put_object", block_manifest)
    env.client.put_commits_before_hang = True
    work = asyncio.create_task(env.workflow.capture(env.ctx, env.key, env.source))
    await asyncio.wait_for(env.client.entered.wait(), 1)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert (
        PUBLISH in (await env.ctx.snapshot()).context
        and COMMITTED not in (await env.ctx.snapshot()).context
    )
    assert env.publisher._gate.stats.in_flight == 0
    assert env.collector.gate.stats.in_flight == 0
    writes = list(env.client.writes)
    verified = await env.workflow.capture(env.ctx, env.key, env.source)
    assert verified.projection.document.markdown == "Source text"
    assert env.client.writes == writes and env.server.posts == 1


@pytest.mark.parametrize("marker", sorted(MARKERS))
async def test_corrupt_or_committed_markers_are_retained_on_force_delete(
    capture_environment, marker
):
    env = capture_environment
    await env.ctx.put(**{marker: None})
    await env.ctx.store.transition(env.ctx.task_id, TaskStatus.FAILED)
    with pytest.raises(TaskBusy):
        await env.ctx.store.delete(
            env.ctx.task_id, force=True, guard=guard_capture_task_deletion
        )


def test_source_lock_manifest_is_explicit_strict_and_bounded(tmp_path):
    value = {
        "kb_id": "kb",
        "source_id": "a" * 64,
        "source_sha256": "b" * 64,
        "size": 50,
        "pages": 7,
        "filename": "source.pdf",
        "approval_id": "review-7",
    }
    lock = SourceLock.from_payload(value)
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"version": 1, "sources": [value]}))
    assert load_source_locks(path) == (lock,)
    for bad in (
        {**value, "pages": True},
        {**value, "size": 0},
        {**value, "filename": "../source.pdf"},
        {**value, "approval_id": ""},
        {**value, "extra": "ignored?"},
    ):
        with pytest.raises(ValueError):
            SourceLock.from_payload(bad)
    path.write_text(json.dumps({"version": 1, "sources": [value, value]}))
    with pytest.raises(ValueError, match="重复"):
        load_source_locks(path)


@pytest.mark.parametrize("changed", ["endpoint", "store"])
async def test_live_binding_mutation_cannot_reuse_committed_evidence(
    capture_environment, changed
):
    env = capture_environment
    await env.workflow.capture(env.ctx, env.key, env.source)
    if changed == "endpoint":
        env.collector.base_url = "http://different.test"
    else:
        env.client.meta.endpoint_url = "http://different-storage.test"
    with pytest.raises(ValueError, match="绑定发生变化"):
        await env.workflow.read_committed(env.ctx, env.key)


@pytest.mark.parametrize(
    "fault",
    ["missing_lock", "null_remote", "false_submit", "source_without_remote", "legacy"],
)
async def test_malformed_recovery_facts_do_not_authorize_network(
    capture_environment, fault
):
    env = capture_environment
    if fault == "legacy":
        await env.ctx.put(mineru_v1_intent=None)
        error = CaptureUncertain
    else:
        error = ValueError
        if fault != "missing_lock":
            await env.workflow.journal(env.ctx, env.key).establish()
        values = (
            {REMOTE: None}
            if fault in {"missing_lock", "null_remote"}
            else {SUBMIT: {"authorized": 1}}
            if fault == "false_submit"
            else {SOURCE: {"task_id": "job", "source_file_id": "file"}}
        )
        await env.ctx.put(**values)
    calls = list(env.server.calls)
    with pytest.raises(error):
        await env.workflow.capture(env.ctx, env.key, env.source)
    assert env.server.calls == calls


async def test_committed_bundle_recovers_without_reloading_deleted_source(
    capture_environment,
):
    env = capture_environment
    await env.workflow.capture(env.ctx, env.key, env.source)
    env.source.path.unlink()

    class MissingLoader:
        async def load(self, source):
            pytest.fail("已提交的包不应重新加载来源")

    result = await env.workflow.extract(
        env.ctx, env.key, MissingLoader(), env.source.source
    )
    assert result.projection.document.markdown == "Source text"


async def test_persisted_context_budget_rejects_before_post(capture_environment):
    env = capture_environment
    env.workflow._limit = 128
    with pytest.raises(ValueError, match="预算"):
        await env.workflow.capture(env.ctx, env.key, env.source)
    assert env.server.posts == 0
