import asyncio

from docx import Document

from comet_rag.engines.converters.base_converter import BaseConverter
from comet_rag.engines.converters.types import DocxDocument


class TextConverter(BaseConverter):
    pass


class DocxConverter(BaseConverter):
    def to_docx(self) -> DocxDocument:
        docx = Document(str(self.result.path))
        return DocxDocument(elements=docx, metadata=self.result.metadata)

    async def ato_docx(self) -> DocxDocument:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.to_docx)
