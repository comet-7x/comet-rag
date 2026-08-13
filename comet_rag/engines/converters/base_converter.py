import asyncio

from comet_rag.engines.converters.types import BaseDocument, ByteDocument
from comet_rag.engines.loaders.base_loader import LoaderContent


class BaseConverter:
    def __init__(self, loader_content: LoaderContent) -> None:
        self.loader_content = loader_content

    def to_bytes(self) -> ByteDocument:
        data = self.loader_content.path.read_bytes()
        return ByteDocument(elements=data, metadata=self.loader_content.metadata)

    async def ato_bytes(self) -> ByteDocument:
        return await asyncio.to_thread(self.to_bytes)

    def to_text(self, encoding: str = "utf-8") -> BaseDocument[str]:
        text = self.loader_content.path.read_text(encoding=encoding)
        return BaseDocument[str](elements=text, metadata=self.loader_content.metadata)

    async def ato_text(self, encoding: str = "utf-8") -> BaseDocument[str]:
        return await asyncio.to_thread(self.to_text)
