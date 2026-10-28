from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import httpx
import pytest

from comet_rag.api.main import create_app
from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import NormalizedDocument
from comet_rag.services.evidence_access import (
    EvidenceAccessDenied,
    EvidenceNotFound,
    EvidenceReader,
    EvidenceReadGrant,
    EvidenceUnavailable,
)
from comet_rag.services.indexing import IndexPlanner, RevisionStager
from tests.unit.services.test_mineru_capture import (
    capture_environment as capture_environment,
)
from tests.unit.services.test_revision_indexing import FakeEmbedding
from tests.unit.test_bootstrap import FakeEmbedding as AppEmbedding
from tests.unit.test_bootstrap import make_config

TOKEN = "unit-test-private-evidence-token"  # noqa: S105 — 只用于内存替身鉴权断言


@pytest.fixture
async def evidence_environment(capture_environment):
    env = capture_environment
    verified = await env.workflow.capture(env.ctx, env.key, env.source)
    docs = InMemoryDocumentStore()
    vectors = AsyncInMemoryVectorStore()
    stage = RevisionStager(
        document_store=docs, vector_store=vectors, embedding_model=FakeEmbedding()
    )
    hierarchy = HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(3)).split(
        NormalizedDocument(verified.projection.document.markdown)
    )
    plan = IndexPlanner().plan(
        hierarchy,
        key=env.key,
        source=str(env.source.path),
        file_type="pdf",
        evidence=verified,
    )
    handoff = await stage.prepare(plan)
    await stage.resume(handoff, embedding_dim=2)
    await docs.activate_revision(env.key)
    env.reader = EvidenceReader(
        documents=docs,
        stores={"documents_v1": env.publisher},
        grants=[
            EvidenceReadGrant(sha256(TOKEN.encode()).hexdigest(), frozenset({"kb"}))
        ],
    )
    env.docs, env.parent_id, env.plan = docs, plan.parents[0].id, plan
    return env


@pytest.mark.parametrize("token,kb", [("wrong", "kb"), (TOKEN, "other"), ("", "kb")])
async def test_authorization_precedes_all_document_and_object_io(
    evidence_environment, token, kb, monkeypatch
):
    env = evidence_environment

    async def forbidden(*args):
        pytest.fail("未授权请求不能触发存储访问")

    monkeypatch.setattr(env.docs, "get_parents", forbidden)
    writes = list(env.client.calls)
    with pytest.raises(EvidenceAccessDenied):
        await env.reader.read(token=token, kb_id=kb, parent_id=env.parent_id)
    assert env.client.calls == writes


async def test_authorized_read_revalidates_parent_and_returns_only_its_source_text(
    evidence_environment,
):
    env = evidence_environment
    result = await env.reader.read(token=TOKEN, kb_id="kb", parent_id=env.parent_id)
    assert result.text == "Source"
    assert (result.start_char, result.end_char) == (0, 6)
    assert (
        result.canonical_sha256
        == env.plan.parents[0].metadata["evidence_reference"]["bundle"][
            "canonical_sha256"
        ]
    )
    assert result.source_id == env.key.source_id and result.revision_id == "revision"
    assert {span.page_number for span in result.evidence} == {1}


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_revision",
        "changed_text",
        "unknown_store",
        "corrupt_bundle",
        "incomplete_active",
    ],
)
async def test_evidence_read_never_falls_back_to_unverified_parent_text(
    evidence_environment, monkeypatch, fault
):
    env = evidence_environment
    original = env.docs.get_parents

    async def damaged(kb_id, ids):
        parents = await original(kb_id, ids)
        result = {}
        for pid, parent in parents.items():
            metadata = dict(parent.metadata)
            if fault in {"unknown_store", "wrong_revision"}:
                reference = metadata["evidence_reference"]
                if fault == "unknown_store":
                    reference["storage_id"] = "unconfigured"
                else:
                    reference["bundle"]["key"]["revision_id"] = "other"
                metadata["evidence_reference"] = reference
            result[pid] = replace(
                parent,
                metadata=metadata,
                text="CHANGED" if fault == "changed_text" else parent.text,
            )
        return result

    monkeypatch.setattr(env.docs, "get_parents", damaged)
    if fault == "corrupt_bundle":
        key = next(k for k in env.client.objects if k.endswith("/ledger.json"))
        env.client.objects[key] = b'{"forged":true}'
    elif fault == "incomplete_active":

        async def incomplete(*args):
            return {}

        monkeypatch.setattr(env.docs, "get_active_revisions", incomplete)
    with pytest.raises(EvidenceUnavailable, match="暂不可用"):
        await env.reader.read(token=TOKEN, kb_id="kb", parent_id=env.parent_id)


async def test_retired_parent_is_not_served_and_pointer_change_during_read_is_rechecked(
    evidence_environment, monkeypatch
):
    env = evidence_environment
    original = env.docs.get_active_revisions
    reads = 0

    async def switched(*args):
        nonlocal reads
        reads += 1
        return await original(*args) if reads == 1 else {env.key.source_id: "newer"}

    monkeypatch.setattr(env.docs, "get_active_revisions", switched)
    with pytest.raises(EvidenceNotFound):
        await env.reader.read(token=TOKEN, kb_id="kb", parent_id=env.parent_id)
    assert reads == 2


async def test_authorized_http_entry_uses_context_and_never_exposes_raw_assets(
    evidence_environment,
):
    env = evidence_environment
    app = create_app(
        make_config(),
        embedding_model=AppEmbedding(),
        document_store=env.docs,
        evidence_reader=env.reader,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://app.test"
        ) as client,
    ):
        url = f"/evidence/kb/parents/{env.parent_id}"
        missing = await client.get(url)
        assert (
            missing.status_code == 401
            and missing.headers["www-authenticate"] == "Bearer"
        )
        denied = await client.get(url, headers={"Authorization": "Bearer wrong"})
        assert denied.status_code == 403
        other = await client.get(
            url.replace("/kb/", "/other/"), headers={"Authorization": "Bearer " + TOKEN}
        )
        assert other.status_code == 403
        accepted = await client.get(url, headers={"Authorization": "Bearer " + TOKEN})
        assert accepted.status_code == 200 and accepted.json()["text"] == "Source"
        assert accepted.headers["cache-control"] == "no-store"
        assert (
            "image_source" not in accepted.text
            and "data:image" not in accepted.text
            and str(env.source.path) not in accepted.text
        )


async def test_http_entry_defaults_to_disabled_and_real_task_service_preserves_intents():
    from comet_rag.services.mineru_capture import SUBMIT  # noqa: PLC0415
    from comet_rag.tasks import TaskStatus  # noqa: PLC0415

    app = create_app(make_config(), embedding_model=AppEmbedding())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://app.test"
        ) as client,
    ):
        assert (await client.get("/evidence/kb/parents/parent")).status_code == 404
        task = await app.state.ctx.task_store.create(
            "retained-intent", context={SUBMIT: None}
        )
        await app.state.ctx.task_store.transition(task.task_id, TaskStatus.FAILED)
        from comet_rag.tasks import TaskBusy  # noqa: PLC0415

        with pytest.raises(TaskBusy):
            await app.state.ctx.task_service.delete(task.task_id, force=True)
        assert await app.state.ctx.task_store.get(task.task_id) is not None
