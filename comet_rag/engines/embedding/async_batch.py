from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from comet_rag.engines.concurrency import map_concurrently
from comet_rag.ports.embedding import AsyncEmbeddingPort

from .batch import DEFAULT_MODEL_BATCH_CONCURRENCY, _joined, _plan


async def embed_documents(
    model: AsyncEmbeddingPort,
    documents: Sequence[str],
    /,
    *,
    max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
    **kwargs: Any,
) -> list[list[float]]:
    """异步批量生成文档向量，结果顺序与输入一致。

    每一块都是一次真实请求，各自穿过模型的进程级闸门；这里的 worker 只额外
    压住**本次调度**的宽度。
    """
    blocks = _plan(model, documents, max_concurrency)
    if not blocks:
        return []

    async def embed_block(block: Sequence[str]) -> list[list[float]]:
        return await model.embed_batch(block, **kwargs)

    return _joined(
        await map_concurrently(embed_block, blocks, limit=max_concurrency),
        len(documents),
    )


__all__ = ["embed_documents"]
