"""批量嵌入的排程。

模型只声明"一次请求最多装几篇"（`EmbeddingPort.batch_limit`），
"发几个请求、几个并发"由这里决定 —— 那取决于调用方在干什么。
"""

from .batch import DEFAULT_MODEL_BATCH_CONCURRENCY, aembed_documents, embed_documents

__all__ = [
    "DEFAULT_MODEL_BATCH_CONCURRENCY",
    "aembed_documents",
    "embed_documents",
]
