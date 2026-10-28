from __future__ import annotations

from typing import Protocol

from comet_rag.documents.schemas import ParsedDocument
from comet_rag.embedder.types import EmbeddingSpace

from .types import BuildResult


class IndexBuilder(Protocol):
    def profile_id(self, space: EmbeddingSpace) -> str: ...

    def build(
        self, document: ParsedDocument, *, index_scope: str, index_profile_id: str
    ) -> BuildResult: ...
