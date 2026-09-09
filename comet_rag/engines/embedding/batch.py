"""按模型批量能力分组请求，并有界调度不支持原生批量的模型。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from comet_rag.engines.defaults import DEFAULT_EMBED_FANOUT
from comet_rag.ports.embedding import EmbeddingPort

#: 兼容旧名。约束的是**一次调度内**的并发，进程级总量由闸门另行控制 ——
#: 两者是不同的旋钮，叠加生效。数字与理由都在 `engines/defaults.py`。
DEFAULT_MODEL_BATCH_CONCURRENCY = DEFAULT_EMBED_FANOUT


def _plan(
    model: EmbeddingPort, documents: Sequence[str], max_concurrency: int
) -> list[Sequence[str]]:
    """按模型声明的 ``batch_limit`` 把文档切成"每块恰好一次请求"。"""
    if max_concurrency <= 0:
        raise ValueError(f"max_concurrency 必须大于 0，收到 {max_concurrency}")
    limit = model.batch_limit
    if limit <= 0:
        raise ValueError(f"{type(model).__name__}.batch_limit 必须大于 0，收到 {limit}")
    return [documents[i : i + limit] for i in range(0, len(documents), limit)]


def _joined(
    blocks: Sequence[Sequence[list[float]]], expected: int
) -> list[list[float]]:
    vectors = [vector for block in blocks for vector in block]
    if len(vectors) != expected:
        raise ValueError(
            f"批量嵌入返回 {len(vectors)} 个向量，但请求了 {expected} 篇文档 —— "
            f"结果无法与输入对齐"
        )
    return vectors


def embed_documents(
    model: EmbeddingPort,
    documents: Sequence[str],
    /,
    *,
    max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
    **kwargs: Any,
) -> list[list[float]]:
    """批量生成文档向量，结果顺序与输入一致。

    同步路径用线程池并发：适配器的同步客户端会阻塞，不开线程就是纯串行。
    线程池开在这里而不是模型里 —— 模型不该替调用方决定要不要起线程。
    """
    blocks = _plan(model, documents, max_concurrency)
    if not blocks:
        return []
    if len(blocks) == 1:
        return _joined([model.embed_batch(blocks[0], **kwargs)], len(documents))

    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    with ThreadPoolExecutor(max_workers=min(max_concurrency, len(blocks))) as executor:
        futures = [
            executor.submit(model.embed_batch, block, **kwargs) for block in blocks
        ]
        return _joined([future.result() for future in futures], len(documents))


async def aembed_documents(
    model: EmbeddingPort,
    documents: Sequence[str],
    /,
    *,
    max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
    **kwargs: Any,
) -> list[list[float]]:
    """异步批量生成文档向量，结果顺序与输入一致。

    每一块都是一次真实请求，各自穿过模型的进程级闸门；这里的信号量只额外
    压住**本次调度**的宽度。
    """
    blocks = _plan(model, documents, max_concurrency)
    if not blocks:
        return []

    semaphore = asyncio.Semaphore(min(max_concurrency, len(blocks)))

    async def _limited(block: Sequence[str]) -> list[list[float]]:
        async with semaphore:
            return await model.aembed_batch(block, **kwargs)

    return _joined(
        await asyncio.gather(*[_limited(block) for block in blocks]), len(documents)
    )


__all__ = [
    "DEFAULT_MODEL_BATCH_CONCURRENCY",
    "aembed_documents",
    "embed_documents",
]
