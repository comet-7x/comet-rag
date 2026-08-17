"""应用层向外部能力提出的稳定接口。

这里**只有契约**。实现基类住在 `comet_rag.infrastructure.models`：
适配器继承它们是为了复用实现，不是为了满足契约 —— Port 是 Protocol，
形状对得上就算实现。
"""

from .embedding import (
    EmbeddingPort,
    EmbeddingTask,
    MultimodalEmbeddingPort,
)
from .gate import AsyncGate
from .reranker import RerankerPort

__all__ = [
    "AsyncGate",
    "EmbeddingPort",
    "EmbeddingTask",
    "MultimodalEmbeddingPort",
    "RerankerPort",
]
