from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class EmbeddingTokenizerResponse(BaseModel):
    count: int = Field(..., description="tokens数量")
    max_model_len: int = Field(..., description="模型最大 embedding 数量")
    tokens: list[int] = Field(..., description="tokens列表")


class BaseEmbeddingModel(ABC):
    @abstractmethod
    def embed(self, *args, **kwargs) -> Any: ...

    @abstractmethod
    async def aembed(self, *args, **kwargs) -> Any: ...

    def batch_embed(
        self,
        embedding_data_list: list,
        *,
        max_concurrency: int = 16,
        **kwargs,
    ) -> list:
        from concurrent.futures import ThreadPoolExecutor

        max_workers = max(1, min(max_concurrency, len(embedding_data_list)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self.embed, data, **kwargs)
                for data in embedding_data_list
            ]
            return [future.result() for future in futures]

    async def abatch_embed(
        self,
        embedding_data_list: list,
        *,
        max_concurrency: int = 16,
        **kwargs,
    ) -> list:
        import asyncio

        semaphore = asyncio.Semaphore(
            max(1, min(max_concurrency, len(embedding_data_list)))
        )

        async def _limited(data):
            async with semaphore:
                return await self.aembed(data, **kwargs)

        return await asyncio.gather(*[_limited(d) for d in embedding_data_list])

    @abstractmethod
    async def close_client(self) -> None: ...
