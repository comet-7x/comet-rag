from __future__ import annotations

from typing import Any

import pytest

from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel


class EchoEmbedding(BaseEmbeddingModel):
    def embed(self, data: Any, **kwargs: Any) -> Any:
        return data

    async def _aembed(self, data: Any, /, **kwargs: Any) -> Any:
        return data


@pytest.mark.parametrize("max_concurrency", [0, -1])
async def test_batch_embedding_rejects_non_positive_concurrency(
    max_concurrency: int,
) -> None:
    model = EchoEmbedding()

    with pytest.raises(ValueError, match="max_concurrency 必须大于 0"):
        model.batch_embed(["text"], max_concurrency=max_concurrency)
    with pytest.raises(ValueError, match="max_concurrency 必须大于 0"):
        await model.abatch_embed(["text"], max_concurrency=max_concurrency)


async def test_empty_batch_returns_without_creating_workers() -> None:
    model = EchoEmbedding()

    assert model.batch_embed([]) == []
    assert await model.abatch_embed([]) == []


async def test_resource_free_model_uses_noop_close() -> None:
    await EchoEmbedding().aclose()
