from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class BaseModelBackend(ABC):
    """LLM 后端基类"""

    @abstractmethod
    async def agenerate(self, prompt: str, **kwargs) -> str:
        """异步生成"""
        ...

    @abstractmethod
    async def agenerate_stream(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        """异步流式生成"""
        ...


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
