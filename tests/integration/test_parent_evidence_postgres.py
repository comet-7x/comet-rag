from __future__ import annotations

from dataclasses import replace

import pytest

from comet_rag.infrastructure.persistence.document_store.postgres import (
    PostgresDocumentStore,
)
from comet_rag.infrastructure.persistence.knowledge_base.postgres import (
    PostgresKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.vector_store.memory import (
    AsyncInMemoryVectorStore,
)
from comet_rag.ports.document_store import RevisionConflict, RevisionStatus
from comet_rag.services.evidence import ParentEvidenceReference
from comet_rag.services.indexing import RevisionStager
from tests.contracts.knowledge_base import make_kb
from tests.integration.conftest import truncate_tables
from tests.unit.services.test_parent_evidence import KEY, evidence_fixture, make_plan
from tests.unit.services.test_revision_indexing import FakeEmbedding

pytestmark = pytest.mark.integration


async def test_parent_evidence_survives_lost_commit_reply_and_new_connection(
    database, postgres_dsn, monkeypatch
) -> None:
    await truncate_tables(database, "knowledge_bases")
    await PostgresKnowledgeBaseRepository(database).create(
        make_kb(KEY.kb_id, embedding_dim=2)
    )
    documents = PostgresDocumentStore(database)
    embedding = FakeEmbedding()
    vectors = AsyncInMemoryVectorStore()
    stager = RevisionStager(
        document_store=documents, vector_store=vectors, embedding_model=embedding
    )
    plan = make_plan()
    original = documents.put_parents
    once = True

    async def lose_response(key, parents):
        nonlocal once
        await original(key, parents)
        if once:
            once = False
            raise OSError("injected parent commit reply loss")

    monkeypatch.setattr(documents, "put_parents", lose_response)
    with pytest.raises(OSError, match="reply loss"):
        await stager.prepare(plan)
    assert embedding.calls == 0
    other = Database(postgres_dsn)
    try:
        reopened = PostgresDocumentStore(other)
        resumed = RevisionStager(
            document_store=reopened, vector_store=vectors, embedding_model=embedding
        )
        handoff = await resumed.prepare(plan)
        stored = await reopened.get_parents(KEY.kb_id, [p.id for p in plan.parents])
        expected, _ = evidence_fixture()
        assert all(
            ParentEvidenceReference.from_payload(p.metadata["evidence_reference"])
            == expected.reference
            for p in stored.values()
        )
        parent = next(iter(stored.values()))
        snapshot = parent.metadata["evidence_reference"]
        assert isinstance(snapshot, dict)
        snapshot["storage_id"] = "mutated_snapshot"
        again = await reopened.get_parents(KEY.kb_id, [parent.parent_id])
        restored = again[parent.parent_id].metadata["evidence_reference"]
        assert isinstance(restored, dict)
        assert restored["storage_id"] == "documents_v1"

        get_parents = reopened.get_parents

        async def corrupt_read(kb_id, ids):
            values = await get_parents(kb_id, ids)
            return {
                pid: replace(p, metadata={**p.metadata, "evidence_reference": None})
                for pid, p in values.items()
            }

        monkeypatch.setattr(reopened, "get_parents", corrupt_read)
        with pytest.raises(RevisionConflict, match="证据引用损坏"):
            await resumed.resume(handoff, embedding_dim=2)
        assert embedding.calls == 0
        monkeypatch.setattr(reopened, "get_parents", get_parents)
        ready = await resumed.resume(handoff, embedding_dim=2)
        assert ready.status is RevisionStatus.READY
        assert all(
            "evidence_reference" not in c.metadata for c in handoff.vector_children
        )
    finally:
        await other.close()
