"""InMemoryDocumentStore 运行 DocumentStore 共享契约。"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.ports.document_store import DocumentStore
from tests.contracts.document_store import DocumentStoreContract


class TestInMemoryDocumentStore(DocumentStoreContract):
    @pytest.fixture
    async def store(self) -> DocumentStore:
        return InMemoryDocumentStore()
