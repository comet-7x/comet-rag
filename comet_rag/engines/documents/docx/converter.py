from __future__ import annotations

import asyncio

from docx import Document

from comet_rag.engines.documents.common.archive import (
    ArchiveLimits,
    validate_zip_archive,
)
from comet_rag.engines.documents.docx.types import DocxDocument
from comet_rag.ports.source import LoadedResource

LoaderContent = LoadedResource


class DocxConverter:
    def __init__(
        self,
        loader_content: LoaderContent,
        *,
        archive_limits: ArchiveLimits | None = None,
    ) -> None:
        self.loader_content = loader_content
        self._archive_limits = archive_limits or ArchiveLimits()

    def to_docx(self) -> DocxDocument:
        validate_zip_archive(self.loader_content.path, self._archive_limits)
        docx = Document(str(self.loader_content.path))
        return DocxDocument(elements=docx, metadata=self.loader_content.metadata)

    async def ato_docx(self) -> DocxDocument:
        return await asyncio.to_thread(self.to_docx)


__all__ = ["DocxConverter"]
