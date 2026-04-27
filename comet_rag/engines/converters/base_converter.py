import asyncio
from abc import ABC

from comet_rag.engines.converters.types import BaseDocument, ByteDocument
from comet_rag.engines.loaders.base_loader import LoaderResult


class BaseConverter(ABC):
    def __init__(self, result: LoaderResult) -> None:
        self.result = result

    def to_bytes(self) -> ByteDocument:
        data = self.result.path.read_bytes()
        return ByteDocument(elements=data, metadata=self.result.metadata)

    async def ato_bytes(self) -> ByteDocument:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.to_bytes)

    def to_text(self) -> BaseDocument[str]:
        text = self.result.path.read_text()
        return BaseDocument(elements=text, metadata=self.result.metadata)

    async def ato_text(self) -> BaseDocument[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.to_text)
