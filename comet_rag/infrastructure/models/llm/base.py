from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class BaseModelBackend(ABC):
    @abstractmethod
    async def agenerate(self, prompt: str, **kwargs) -> str: ...

    @abstractmethod
    async def agenerate_stream(self, prompt: str, **kwargs) -> AsyncIterator[str]: ...


class ModelFactory:
    @staticmethod
    def create(
        url: str,
        model_platform: str,
        model_type: str,
        api_key: str,
        model_config_dict: dict[str, Any] | None = None,
    ) -> BaseModelBackend:
        # TODO: 根据 model_platform 创建对应实现
        raise NotImplementedError("Model factory not implemented")
