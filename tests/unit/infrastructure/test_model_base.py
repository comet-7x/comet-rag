from __future__ import annotations

from typing import Any

import pytest

from comet_rag.application.ports import (
    EmbeddingPort,
    MultimodalEmbeddingPort,
    RerankerPort,
)
from comet_rag.infrastructure.models.embedding.base import (
    BaseEmbeddingModel,
    MultimodalEmbeddingMixin,
)
from comet_rag.infrastructure.models.reranker.base import BaseReranker


class EchoEmbedding(BaseEmbeddingModel):
    def embed(self, data: Any, **kwargs: Any) -> Any:
        return data

    async def _aembed(self, data: Any, /, **kwargs: Any) -> Any:
        return data


class EchoMultimodalEmbedding(MultimodalEmbeddingMixin, EchoEmbedding):
    def embed_media(self, data: Any, /, **kwargs: Any) -> Any:
        return data

    async def _aembed_media(self, data: Any, /, **kwargs: Any) -> Any:
        return data


class EchoReranker(BaseReranker[str]):
    def score(self, query: str, documents: Any, **kwargs: Any) -> list[float]:
        return [0.0] * len(list(documents))

    async def _ascore(
        self, query: str, documents: Any, **kwargs: Any
    ) -> list[float]:
        return [0.0] * len(list(documents))


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


# ── 契约与实现的分界 ───────────────────────────────────────────────────────


def test_base_classes_satisfy_their_ports() -> None:
    """基类不继承 Port，所以"是否满足契约"必须另有人来验。

    Port 是 Protocol，实现侧靠**形状**匹配。少一个方法不会有任何继承层面的
    报错，只会在装配处的类型检查里冒出来 —— 而那要等到有人真去改 bootstrap。
    这条用例把两个方向都钉住：基类实现了契约，Port 也没有偷偷长出基类没有
    的方法。
    """
    assert isinstance(EchoEmbedding(), EmbeddingPort)
    assert isinstance(EchoReranker(), RerankerPort)


def test_multimodal_capability_is_structural_not_advertised() -> None:
    """**纯文本模型不得通过多模态能力检查。**

    `MultimodalEmbeddingPort` 是 runtime_checkable 的，而 Protocol 的
    `isinstance` 只看方法在不在、不看它做什么。所以只要基类给纯文本模型留
    一个"默认抛 TypeError"的 `embed_media`，这个检查就永远返回 True ——
    调用方据此分发，然后在运行时炸掉，协议等于没有。

    能力做成 mixin 之后，答案是结构性的：不继承就真的没有那个方法。
    """
    assert not isinstance(EchoEmbedding(), MultimodalEmbeddingPort)
    assert isinstance(EchoMultimodalEmbedding(), MultimodalEmbeddingPort)
