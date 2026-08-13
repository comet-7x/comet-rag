import asyncio

from docx import Document

from comet_rag.engines.converters.archive_guard import (
    ArchiveLimits,
    validate_zip_archive,
)
from comet_rag.engines.converters.base_converter import BaseConverter
from comet_rag.engines.converters.types import DocxDocument
from comet_rag.engines.loaders.types import LoaderContent


class TextConverter(BaseConverter):
    pass


class DocxConverter(BaseConverter):
    def __init__(
        self,
        loader_content: LoaderContent,
        *,
        archive_limits: ArchiveLimits | None = None,
    ) -> None:
        super().__init__(loader_content)
        self._archive_limits = archive_limits or ArchiveLimits()

    def to_docx(self) -> DocxDocument:
        validate_zip_archive(self.loader_content.path, self._archive_limits)
        docx = Document(str(self.loader_content.path))
        return DocxDocument(elements=docx, metadata=self.loader_content.metadata)

    async def ato_docx(self) -> DocxDocument:
        return await asyncio.to_thread(self.to_docx)
