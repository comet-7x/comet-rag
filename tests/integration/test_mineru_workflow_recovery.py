from __future__ import annotations

import os
from hashlib import sha256

import httpx
import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.engines.pipelines import AsyncPipelineHooks, PipelineConfig
from comet_rag.infrastructure.evidence.s3 import AsyncS3EvidenceStore, S3EvidenceError
from comet_rag.infrastructure.extractors.mineru_capture import (
    AsyncMinerUEvidenceCapture,
)
from comet_rag.infrastructure.extractors.mineru_v1 import (
    AsyncMinerUV1Collector,
    CaptureSource,
)
from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.task_store.postgres import PostgresTaskStore
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.infrastructure.sources import AsyncLocalLoader, SourceContent
from comet_rag.ports.capture import SourceLock
from comet_rag.services.evidence_access import EvidenceReader, EvidenceReadGrant
from comet_rag.services.indexing import RevisionStager
from comet_rag.services.ingestion import DocumentExtraction
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.mineru_capture import COMMITTED, PUBLISH, MinerUWorkflow
from comet_rag.services.revision_context import RevisionTaskStages, load_task_handoff
from comet_rag.services.revision_gc import RevisionGarbageCollector
from comet_rag.services.revision_ingestion import (
    REVISION_INGEST_KIND,
    RevisionIngestRunner,
)
from comet_rag.tasks import Done, Handoff, TaskContext, TaskStatus
from tests.contracts.knowledge_base import make_kb
from tests.fixtures.pdf import build_minimal_pdf
from tests.integration.conftest import truncate_tables
from tests.integration.test_s3_evidence_minio import scope as scope
from tests.unit.services.test_revision_indexing import FakeEmbedding
from tests.unit.test_mineru_v1_capture import Server, limits

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("fault", ["manifest_reply", "checkpoint_reply"])
async def test_real_stores_resume_capture_then_handoff_and_lost_activation_reply(
    scope, database, postgres_dsn, tmp_path, monkeypatch, fault
):
    if os.environ.get("COMET_TEST_S3_CONDITIONAL") != "1":
        pytest.skip("需显式使用已验收条件写的独立S3命名空间")
    tracked, publisher, bucket = scope
    await publisher.verify_target()
    await truncate_tables(database, "tasks", "knowledge_bases")
    repository = PostgresKnowledgeBaseRepository(database)
    await repository.create(make_kb("kb", embedding_model="test", embedding_dim=2))
    path = build_minimal_pdf(tmp_path / "source.pdf")
    origin = SourceContent(str(path))
    lock = SourceLock(
        "kb",
        origin.source_id,
        sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
        1,
        "sample.pdf",
        "generated-one-page-fixture",
    )
    server = Server(
        CaptureSource(path, lock.filename, lock.source_sha256, lock.size, 1)
    )
    vectors, embedding = AsyncInMemoryVectorStore(), FakeEmbedding()
    config, hooks = (
        PipelineConfig(chunk_size=3, chunk_overlap=0),
        AsyncPipelineHooks.fork(),
    )
    tasks = PostgresTaskStore(database)
    task = await tasks.create(
        REVISION_INGEST_KIND,
        request={
            "kb_id": "kb",
            "source": str(path),
            "revision_binding": "native-store-test",
        },
    )

    async def claim(store, worker, attempts, stage):
        current = await store.require(task.task_id)
        if current.status is TaskStatus.RUNNING:
            await store.transition(
                task.task_id, TaskStatus.PENDING, worker_id=None, resume_stage=stage
            )
        await store.transition(
            task.task_id, TaskStatus.RUNNING, worker_id=worker, attempts=attempts
        )
        return TaskContext(
            store,
            task.task_id,
            worker_id=worker,
            attempts=attempts,
            lane="cpu" if "cpu" in worker else "io",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), trust_env=False
    ) as http:
        collector = AsyncMinerUV1Collector(
            "http://localhost",
            client=http,
            gate=Gate(limit=1, max_waiting=2),
            limits=limits(),
        )
        capture = AsyncMinerUEvidenceCapture(collector, temporary_directory=tmp_path)

        def runner(db, store):
            documents = PostgresDocumentStore(db)
            workflow = MinerUWorkflow(
                capture=capture,
                publisher=store,
                storage_id="documents_v1",
                source_locks=[lock],
            )
            stages = RevisionTaskStages(
                RevisionStager(
                    document_store=documents,
                    vector_store=vectors,
                    embedding_model=embedding,
                ),
                document_store=documents,
                garbage_collector=RevisionGarbageCollector(
                    document_store=documents, vector_store=vectors
                ),
            )
            service = RevisionIngestRunner(
                extraction=DocumentExtraction(
                    loader=AsyncLocalLoader(),
                    config=config,
                    hooks=hooks,
                    mineru_workflow=workflow,
                ),
                stages=stages,
                knowledge_base=KnowledgeBaseService(
                    repository=PostgresKnowledgeBaseRepository(db),
                    vector_store=vectors,
                    embedding_model="test",
                    embedding_dim=2,
                ),
                config=config,
                hooks=hooks,
                binding="native-store-test",
                kb_ids=["kb"],
                hierarchy=HierarchyBuilder(FixedSizeChunker(6), FixedSizeChunker(3)),
                mineru_workflow=workflow,
            )
            return service, documents, workflow

        first, _, _ = runner(database, publisher)
        cpu = await claim(tasks, "first-cpu", 1, None)
        if fault == "manifest_reply":
            tracked.lose_manifest = True
        else:
            original = tasks._put_context
            fail = True

            async def lose_checkpoint(task_id, values, **kwargs):
                nonlocal fail
                result = await original(task_id, values, **kwargs)
                if PUBLISH in values and fail:
                    fail = False
                    raise OSError("injected publication intent reply loss")
                return result

            monkeypatch.setattr(tasks, "_put_context", lose_checkpoint)
        with pytest.raises((OSError, S3EvidenceError)):
            await first(cpu)
        assert server.posts == 1 and embedding.calls == 0
        assert PUBLISH in (await tasks.require(task.task_id)).context

        other = Database(postgres_dsn)
        try:
            fresh_tasks = PostgresTaskStore(other)
            reopened_publisher = AsyncS3EvidenceStore(
                client=tracked, bucket=bucket, gate=Gate(limit=1, max_waiting=2)
            )
            if fault == "checkpoint_reply":
                await reopened_publisher.verify_target()
            resumed, documents, workflow = runner(other, reopened_publisher)
            fresh_cpu = await claim(fresh_tasks, "recovered-cpu", 2, "extracting")
            handoff = await resumed(fresh_cpu)
            assert isinstance(handoff, Handoff) and handoff.lane == "io"
            saved = await fresh_tasks.require(task.task_id)
            plan = load_task_handoff(saved.context, task.task_id)
            assert (
                COMMITTED in saved.context
                and server.posts == 1
                and embedding.calls == 0
            )
            parents = await documents.get_parents(
                "kb",
                [
                    child.parent_id
                    for child in plan.vector_children
                    if child.parent_id is not None
                ],
            )
            assert parents and all(
                p.metadata["evidence_reference"] == saved.context[COMMITTED]
                for p in parents.values()
            )

            activate = documents.activate_revision
            lost = False

            async def lose_activation(key):
                nonlocal lost
                result = await activate(key)
                if not lost:
                    lost = True
                    raise OSError("injected active commit reply loss")
                return result

            monkeypatch.setattr(documents, "activate_revision", lose_activation)
            io = await claim(fresh_tasks, "first-io", 3, "indexing")
            with pytest.raises(OSError):
                await resumed(io)
            calls = embedding.calls
            retry = await claim(fresh_tasks, "recovered-io", 4, "activating")
            done = await resumed(retry)
            assert isinstance(done, Done) and embedding.calls == calls
            assert await documents.get_active_revisions("kb", [origin.source_id]) == {
                origin.source_id: plan.draft.key.revision_id
            }
            assert server.posts == 1

            token = "native-store-test-credential"  # noqa: S105 — 隔离测试专用
            reader = EvidenceReader(
                documents=documents,
                stores={"documents_v1": reopened_publisher},
                grants=[
                    EvidenceReadGrant(
                        sha256(token.encode()).hexdigest(), frozenset({"kb"})
                    )
                ],
            )
            view = await reader.read(
                token=token, kb_id="kb", parent_id=next(iter(parents))
            )
            assert view.text in {"Source", " text"}
        finally:
            await other.close()
