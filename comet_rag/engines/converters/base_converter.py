import asyncio

from comet_rag.engines.converters.types import ByteDocument
from comet_rag.engines.loaders.base_loader import LoaderResult


class BaseConverter:
    def __init__(self, result: LoaderResult) -> None:
        self.result = result

    def to_bytes(self) -> ByteDocument:
        bytes = self.result.path.read_bytes()
        metadata = self.result.metadata
        return ByteDocument(elements=bytes, metadata=metadata)

    async def ato_bytes(self) -> ByteDocument:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.to_bytes)
