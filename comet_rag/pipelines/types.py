from __future__ import annotations

from dataclasses import dataclass

from comet_rag.docstores.types import WriteManifest
from comet_rag.documents.schemas import ParseReport


@dataclass(frozen=True, slots=True)
class IngestionResult:
    source_id: str
    document_id: str
    index_scope: str
    index_profile_id: str
    manifest: WriteManifest
    parse_report: ParseReport
    database_committed: bool
    query_visible: bool
