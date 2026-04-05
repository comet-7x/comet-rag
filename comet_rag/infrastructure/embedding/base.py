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
