"""Reranker 模型封装"""

from abc import ABC, abstractmethod


class BaseReranker(ABC):
    """Reranker 模型基类"""

    @abstractmethod
    async def arerank(
        self, query: str, documents: list[str], top_k: int = 5
    ) -> list[int]:
        """异步重排序，返回文档索引列表"""
        raise NotImplementedError
