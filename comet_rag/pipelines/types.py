from __future__ import annotations

from dataclasses import dataclass

from comet_rag.docstores.types import WriteManifest
from comet_rag.documents.schemas import ParseReport
from comet_rag.errors import CometRAGError


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


class IngestionIncomplete(CometRAGError, RuntimeError):
    """内容清单已提交，向量状态需按相同身份核对后才能切换。"""

    def __init__(self, result: IngestionResult) -> None:
        super().__init__(
            f"vector state unknown or incomplete for {result.manifest.manifest_id}"
        )
        self.result = result
