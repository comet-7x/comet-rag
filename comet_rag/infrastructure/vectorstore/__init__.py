"""向量数据库封装"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any


class BaseVectorStore(ABC):
    """向量存储基类"""

    @abstractmethod
    async def aadd_texts(
        self,
        texts: list[str],
        embeddings: list[Sequence[float]],
        metadata: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """异步添加文档"""
        raise NotImplementedError

    @abstractmethod
    async def adelete(self, ids: list[str]) -> None:
        """异步删除文档"""
        raise NotImplementedError

    @abstractmethod
    async def asearch(
        self,
        query_embedding: Sequence[float],
        top_k: int = 5,
        filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """异步搜索"""
        raise NotImplementedError
