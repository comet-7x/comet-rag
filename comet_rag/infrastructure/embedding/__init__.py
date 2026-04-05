from abc import ABC, abstractmethod
from collections.abc import Sequence


class BaseEmbeddingModel(ABC):
    """Embedding 模型基类"""

    @abstractmethod
    async def aembed_text(self, text: str) -> Sequence[float]:
        """异步获取单条文本的向量"""
        raise NotImplementedError

    @abstractmethod
    async def aembed_texts(self, texts: list[str]) -> list[Sequence[float]]:
        """异步获取多条文本的向量"""
        raise NotImplementedError
