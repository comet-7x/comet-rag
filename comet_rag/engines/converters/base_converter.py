import asyncio

from comet_rag.engines.converters.types import BaseDocument, ByteDocument
from comet_rag.engines.loaders.base_loader import LoaderContent


class BaseConverter:
    def __init__(self, result: LoaderContent) -> None:
        self.result = result

    def to_bytes(self) -> ByteDocument:
        data = self.result.path.read_bytes()
        return ByteDocument(elements=data, metadata=self.result.metadata)

    async def ato_bytes(self) -> ByteDocument:
        return await asyncio.to_thread(self.to_bytes)

    def to_text(self) -> BaseDocument[str]:
        text = self.result.path.read_text()
        return BaseDocument[str](elements=text, metadata=self.result.metadata)

    async def ato_text(self) -> BaseDocument[str]:
        return await asyncio.to_thread(self.to_text)
