"""批量嵌入的排程：切块、限流、拼接。

这些用例过去挂在 `BaseEmbeddingModel` 上，随实现一起搬到了 application 层。
它们要盯的东西没变，但**盯的位置**变了 —— 现在 `max_concurrency` 真的只有
一个含义了。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from comet_rag.application.embedding_batch import aembed_documents, embed_documents
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel


class SpyEmbedding(BaseEmbeddingModel):
    """记录每次请求装了几篇，以及真实并发峰值。"""

    def __init__(self, batch_limit: int = 1, delay: float = 0.0) -> None:
        self.batch_limit = batch_limit
        self.delay = delay
        #: 每次真实请求携带的文档数，按发生顺序
        self.request_sizes: list[int] = []
        self.live = 0
        self.peak = 0

    def _vectors(self, documents: Sequence[str]) -> list[list[float]]:
        self.request_sizes.append(len(documents))
        return [[float(len(document))] for document in documents]

    def embed(self, data: str, /, **kwargs: Any) -> list[float]:
        return self._vectors([data])[0]

    async def _aembed(self, data: str, /, **kwargs: Any) -> list[float]:
        return (await self._aembed_batch([data], **kwargs))[0]

    def _embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        return self._vectors(documents)

    async def _aembed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(self.delay)
            return self._vectors(documents)
        finally:
            self.live -= 1


# ── 切块由模型声明的能力决定 ───────────────────────────────────────────────


async def test_batch_limit_decides_how_many_documents_ride_one_request() -> None:
    """**同一次调用，在两类适配器上发出的请求数不同 —— 这正是重点。**

    调度方只说"把这 5 篇发出去，最多 3 个并发"，装几条由模型声明的能力决定。
    """
    single = SpyEmbedding(batch_limit=1)
    native = SpyEmbedding(batch_limit=2)
    documents = ["a", "bb", "ccc", "dddd", "eeeee"]

    assert await aembed_documents(single, documents, max_concurrency=3) == [
        [1.0],
        [2.0],
        [3.0],
        [4.0],
        [5.0],
    ]
    assert await aembed_documents(native, documents, max_concurrency=3) == [
        [1.0],
        [2.0],
        [3.0],
        [4.0],
        [5.0],
    ]

    assert single.request_sizes == [1, 1, 1, 1, 1]
    assert sorted(native.request_sizes) == [1, 2, 2]


def test_sync_path_chunks_the_same_way() -> None:
    model = SpyEmbedding(batch_limit=2)

    assert embed_documents(model, ["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert sorted(model.request_sizes) == [1, 2]


# ── max_concurrency 现在只有一个含义 ───────────────────────────────────────


async def test_max_concurrency_bounds_requests_in_flight_for_native_batching() -> None:
    """**旧实现在这里会静默失效。**

    支持原生批量的适配器走的是 `_aembed_documents_native` 那条分支，
    `max_concurrency` 被收下、被校验，然后完全不起作用。现在它约束的是
    "同时在飞的请求数"，两类适配器一个含义。
    """
    model = SpyEmbedding(batch_limit=2, delay=0.02)

    await aembed_documents(model, [f"doc-{i}" for i in range(12)], max_concurrency=2)

    assert model.request_sizes == [2] * 6
    assert model.peak <= 2, f"限 2，实际同时在飞 {model.peak} 个请求"
    assert model.peak > 1, "峰值恒为 1 说明根本没并发，这条用例测了个寂寞"


@pytest.mark.parametrize("max_concurrency", [0, -1])
async def test_rejects_non_positive_concurrency(max_concurrency: int) -> None:
    model = SpyEmbedding()

    with pytest.raises(ValueError, match="max_concurrency 必须大于 0"):
        embed_documents(model, ["text"], max_concurrency=max_concurrency)
    with pytest.raises(ValueError, match="max_concurrency 必须大于 0"):
        await aembed_documents(model, ["text"], max_concurrency=max_concurrency)


async def test_nonsense_batch_limit_is_refused() -> None:
    model = SpyEmbedding(batch_limit=0)

    with pytest.raises(ValueError, match="batch_limit 必须大于 0"):
        await aembed_documents(model, ["text"])


async def test_empty_input_needs_no_round_trip() -> None:
    model = SpyEmbedding()

    assert embed_documents(model, []) == []
    assert await aembed_documents(model, []) == []
    assert model.request_sizes == []


# ── 拼接 ───────────────────────────────────────────────────────────────────


async def test_result_order_follows_input_across_blocks() -> None:
    """块之间乱序返回也不能错位 —— 错位不报错，只让 chunk 配上别人的向量。"""
    model = SpyEmbedding(batch_limit=1, delay=0.0)

    documents = [f"{'x' * n}" for n in range(1, 21)]
    result = await aembed_documents(model, documents, max_concurrency=8)

    assert result == [[float(n)] for n in range(1, 21)]


async def test_short_result_is_refused_instead_of_silently_misaligning() -> None:
    """适配器少还一个向量时，必须在装配接缝上炸掉。

    放过去的后果是后续 `zip(chunks, embeddings)` 整体错位一位：**每个 chunk
    都配上了下一个 chunk 的向量**，检索结果静静地变差，没有任何报错。
    """

    class ShortChanging(SpyEmbedding):
        async def _aembed_batch(
            self, documents: Sequence[str], /, **kwargs: Any
        ) -> list[list[float]]:
            vectors = await super()._aembed_batch(documents, **kwargs)
            return vectors[:-1] or vectors

    model = ShortChanging(batch_limit=4)

    with pytest.raises(ValueError, match="无法与输入对齐"):
        await aembed_documents(model, ["a", "bb", "ccc", "dddd"])
