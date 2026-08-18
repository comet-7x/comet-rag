"""外部模型服务适配器的便捷入口。

业务代码依赖 :mod:`comet_rag.ports`；直接把项目当库使用时，可以
从这里导入具体适配器，从 :mod:`comet_rag.ports` 导入供应商无关的输入类型。
"""

from .embedding import OpenAIEmbeddingModel, Qwen3VLEmbeddingModel
from .reranker import Qwen3VLReranker
from .vision import OpenAIVisionModel

__all__ = [
    "OpenAIEmbeddingModel",
    "OpenAIVisionModel",
    "Qwen3VLEmbeddingModel",
    "Qwen3VLReranker",
]
