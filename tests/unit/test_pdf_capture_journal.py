from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from comet_rag.infrastructure.evidence.bundle import publish_bundle
from comet_rag.tasks import InMemoryTaskStore, TaskBusy, TaskContext, TaskStatus
from comet_rag.tasks.runner import TaskCancelled
from comet_rag.tasks.store import TaskLeaseLost
from evals.pdf import capture_journal as journal_module
from evals.pdf.capture_journal import (
    BUNDLE_COMMITTED,
    BUNDLE_INTENT,
    INTENT,
    MARKERS,
    OBSERVED,
    AsyncCaptureJournal,
    guard_capture_task_deletion,
    remote_disposition,
)
from tests.unit.test_pdf_evidence_bundle import KEY, SOURCE, packet

ENDPOINT = "c" * 64


async def environment():
    store = InMemoryTaskStore()
    task = await store.create("isolated-mineru-evidence")
    await store.transition(
        task.task_id, TaskStatus.RUNNING, worker_id="worker", attempts=1
    )
    ctx = TaskContext(store, task.task_id, worker_id="worker", attempts=1)
    return ctx, AsyncCaptureJournal(ctx)


async def begun():
    ctx, journal = await environment()
    assert await journal.begin(key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT)
    return ctx, journal


async def verified():
    ctx, journal = await begun()
    intent = (await ctx.snapshot()).context[INTENT]
    await journal.observe(task_id="job_one", source_file_id="file-one", intent=intent)
    await journal.verify_source(observed=(await ctx.snapshot()).context[OBSERVED])
    return ctx, journal


async def test_post_intent_response_loss_never_authorizes_a_second_submission(
    monkeypatch,
) -> None:
    ctx, journal = await environment()
    original = ctx.store._put_context
    fail = True

    async def lose_reply(*args, **kwargs):
        nonlocal fail
        result = await original(*args, **kwargs)
        if fail:
            fail = False
            raise OSError("committed but response lost")
        return result

    monkeypatch.setattr(ctx.store, "_put_context", lose_reply)
    with pytest.raises(OSError, match="response lost"):
        await journal.begin(key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT)
    # 换Context模拟恢复；已存在意图意味着可能发过POST，不能重复授权。
    fresh = AsyncCaptureJournal(
        TaskContext(ctx.store, ctx.task_id, worker_id="worker", attempts=1)
    )
    assert not await fresh.begin(
        key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT
    )


async def test_concurrent_begin_grants_only_one_local_submission_intent() -> None:
    _, journal = await environment()
    values = await asyncio.gather(
        *(
            journal.begin(key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT)
            for _ in range(2)
        )
    )
    assert sorted(values) == [False, True]


@pytest.mark.parametrize("fault", ["source", "endpoint", "observed_task"])
async def test_journal_cannot_replace_a_previous_identity(fault: str) -> None:
    ctx, journal = await begun()
    if fault == "observed_task":
        intent = (await ctx.snapshot()).context[INTENT]
        await journal.observe(
            task_id="job_one", source_file_id="file-one", intent=intent
        )
        with pytest.raises(ValueError, match="不可覆盖"):
            await journal.observe(
                task_id="job_other", source_file_id="file-one", intent=intent
            )
    else:
        with pytest.raises(ValueError, match="不可覆盖"):
            await journal.begin(
                key=KEY,
                source_sha256="f" * 64 if fault == "source" else SOURCE,
                endpoint_sha256="f" * 64 if fault == "endpoint" else ENDPOINT,
            )


@pytest.mark.parametrize("fault", ["unfenced", "old_attempt", "cancelling", "budget"])
async def test_existing_store_fence_and_context_budget_precede_intent(
    fault: str,
) -> None:
    ctx, journal = await environment()
    if fault == "unfenced":
        with pytest.raises(ValueError, match="围栏"):
            AsyncCaptureJournal(TaskContext(ctx.store, ctx.task_id))
        return
    if fault == "old_attempt":
        await ctx.store.update(ctx.task_id, attempts=2)
        error = TaskLeaseLost
    elif fault == "cancelling":
        await ctx.store.transition(ctx.task_id, TaskStatus.CANCELLING)
        error = TaskCancelled
    else:
        await ctx.put(existing="x" * 8192)
        error = ValueError
    with pytest.raises(error):
        await journal.begin(key=KEY, source_sha256=SOURCE, endpoint_sha256=ENDPOINT)
    assert INTENT not in (await ctx.snapshot()).context


@pytest.mark.parametrize("marker", sorted(MARKERS))
async def test_terminal_and_force_cannot_delete_even_corrupt_recovery_marker(
    marker: str,
) -> None:
    ctx, _ = await environment()
    await ctx.put(**{marker: None})
    await ctx.store.transition(ctx.task_id, TaskStatus.FAILED)
    with pytest.raises(TaskBusy):
        await ctx.store.delete(
            ctx.task_id, force=True, guard=guard_capture_task_deletion
        )
    assert await ctx.store.get(ctx.task_id) is not None


async def test_bundle_intent_precedes_publication_and_failed_read_never_confirms(
    tmp_path: Path,
) -> None:
    ctx, journal = await verified()
    bundle = packet()
    await journal.plan_bundle(bundle.reference)
    assert (await ctx.snapshot()).context[BUNDLE_INTENT] == bundle.reference.payload()
    with pytest.raises(FileNotFoundError):
        await journal.commit_local_bundle(bundle.reference, tmp_path / "missing")
    assert BUNDLE_COMMITTED not in (await ctx.snapshot()).context
    publish_bundle(bundle, tmp_path / "bundle")
    await journal.commit_local_bundle(bundle.reference, tmp_path / "bundle")
    assert (await ctx.snapshot()).context[
        BUNDLE_COMMITTED
    ] == bundle.reference.payload()


async def test_bundle_commit_response_loss_recovers_without_republishing(
    tmp_path: Path, monkeypatch
) -> None:
    ctx, journal = await verified()
    bundle = packet()
    await journal.plan_bundle(bundle.reference)
    publish_bundle(bundle, tmp_path / "bundle")
    original = ctx.store._put_context
    fail = True

    async def lose_commit(task_id, values, **kwargs):
        nonlocal fail
        result = await original(task_id, values, **kwargs)
        if BUNDLE_COMMITTED in values and fail:
            fail = False
            raise OSError("confirmation lost")
        return result

    monkeypatch.setattr(ctx.store, "_put_context", lose_commit)
    with pytest.raises(OSError, match="confirmation lost"):
        await journal.commit_local_bundle(bundle.reference, tmp_path / "bundle")
    fresh = AsyncCaptureJournal(
        TaskContext(ctx.store, ctx.task_id, worker_id="worker", attempts=1)
    )
    await fresh.commit_local_bundle(bundle.reference, tmp_path / "bundle")
    assert (await ctx.snapshot()).context[
        BUNDLE_COMMITTED
    ] == bundle.reference.payload()


async def test_unverified_source_cannot_register_a_bundle() -> None:
    ctx, journal = await begun()
    with pytest.raises(ValueError, match="前置身份"):
        await journal.plan_bundle(packet().reference)
    assert BUNDLE_INTENT not in (await ctx.snapshot()).context


async def test_cancelled_bundle_read_is_drained_without_commit(
    tmp_path: Path, monkeypatch
) -> None:
    ctx, journal = await verified()
    bundle = packet()
    await journal.plan_bundle(bundle.reference)
    publish_bundle(bundle, tmp_path / "bundle")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    original = journal_module.load_bundle

    def blocked(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=2)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(journal_module, "load_bundle", blocked)
    work = asyncio.create_task(
        journal.commit_local_bundle(bundle.reference, tmp_path / "bundle")
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done()
        assert BUNDLE_COMMITTED not in (await ctx.snapshot()).context
    finally:
        release.set()
        result = await asyncio.gather(work, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert finished.is_set()
    assert BUNDLE_COMMITTED not in (await ctx.snapshot()).context


def test_invalid_artifact_requires_manual_review_without_automatic_recapture() -> None:
    result = remote_disposition(
        submission="source_verified",
        task_id="job_one",
        source_verified=True,
        remote_status="completed",
        bundle_verified=False,
        artifact_invalid=True,
    )
    assert result.next_step == "manual_reconcile_invalid_artifact"
    assert not result.automatic_resubmit and not result.delete_remote_files


@pytest.mark.parametrize(
    "submission,task_id,verified,status,bundle,missing,expected",
    [
        (
            "not_sent",
            None,
            False,
            None,
            False,
            False,
            "fresh_submission_requires_new_intent",
        ),
        ("unknown", None, False, None, False, False, "manual_reconcile_identity"),
        (
            "task_observed",
            "job_one",
            False,
            "queued",
            False,
            False,
            "manual_reconcile_identity",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "queued",
            False,
            False,
            "bounded_poll_or_explicit_cancel",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "running",
            False,
            False,
            "bounded_poll_or_explicit_cancel",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "completed",
            False,
            False,
            "capture_existing_artifact",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "completed",
            True,
            False,
            "retain_receipt_use_upstream_retention",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "canceled",
            False,
            False,
            "retain_receipt_use_upstream_retention",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "failed",
            False,
            False,
            "retain_receipt_use_upstream_retention",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "partial",
            False,
            False,
            "retain_receipt_use_upstream_retention",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "completed",
            True,
            True,
            "manual_reconcile_restart_or_expiry",
        ),
        (
            "source_verified",
            "job_one",
            True,
            "new_status",
            False,
            False,
            "manual_reconcile_protocol",
        ),
    ],
)
def test_remote_disposition_never_infers_drain_or_owns_remote_bytes(
    submission, task_id, verified, status, bundle, missing, expected
) -> None:
    result = remote_disposition(
        submission=submission,
        task_id=task_id,
        source_verified=verified,
        remote_status=status,
        bundle_verified=bundle,
        not_found=missing,
    )
    assert result.next_step == expected
    assert not result.automatic_resubmit
    assert not result.delete_remote_files
    assert not result.remote_drained
