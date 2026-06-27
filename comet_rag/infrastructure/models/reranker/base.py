from abc import ABC, abstractmethod
from typing import Any


class BaseReranker(ABC):
    @abstractmethod
    def score(self, query: Any, documents: Any, **kwargs) -> list[float]: ...

    @abstractmethod
    async def ascore(self, query: Any, documents: Any, **kwargs) -> list[float]: ...
