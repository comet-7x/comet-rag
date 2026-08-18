"""尚未接入组合根的 LLM 后端契约。

当前生产链路只装配 embedding 与 reranker；这里是后续 LLM 功能的最小协议，
``ModelFactory`` 仍是显式占位，调用会失败，不能把它当成已完成的装配入口。
"""

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
    """LLM 工厂占位；供应商选择完成前不提供静默默认实现。"""

    @staticmethod
    def create(
        url: str,
        model_platform: str,
        model_type: str,
        api_key: str,
        model_config_dict: dict[str, Any] | None = None,
    ) -> BaseModelBackend:
        """创建模型实例"""
        # TODO: 根据 model_platform 创建对应实现
        raise NotImplementedError("Model factory not implemented")
