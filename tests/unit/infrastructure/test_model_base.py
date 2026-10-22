from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from comet_rag.infrastructure.models.embedding.base import (
    AsyncBaseEmbeddingModel,
    AsyncMultimodalEmbeddingMixin,
    BaseEmbeddingModel,
    MultimodalEmbeddingMixin,
)
from comet_rag.infrastructure.models.reranker.base import (
    AsyncBaseReranker,
    BaseReranker,
)
from comet_rag.ports import (
    AsyncEmbeddingPort,
    AsyncMultimodalEmbeddingPort,
    AsyncRerankerPort,
    EmbeddingPort,
    MultimodalEmbeddingPort,
    RerankerPort,
)
from comet_rag.ports.content import ContentInput, MediaResource


class EchoEmbedding(BaseEmbeddingModel):
    def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        return [float(len(data))]


class AsyncEchoEmbedding(AsyncBaseEmbeddingModel):
    async def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        return [float(len(data))]


class EchoMultimodalEmbedding(MultimodalEmbeddingMixin, EchoEmbedding):
    def _embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        return [1.0]


class AsyncEchoMultimodalEmbedding(AsyncMultimodalEmbeddingMixin, AsyncEchoEmbedding):
    async def _embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        return [1.0]


class EchoReranker(BaseReranker[str]):
    def _score(
        self, query: str, documents: Sequence[str], **kwargs: Any
    ) -> list[float]:
        return [0.0] * len(documents)


class AsyncEchoReranker(AsyncBaseReranker[str]):
    async def _score(
        self, query: str, documents: Sequence[str], **kwargs: Any
    ) -> list[float]:
        return [0.0] * len(documents)


async def test_empty_batch_needs_no_round_trip() -> None:
    assert EchoEmbedding().embed_batch([]) == []
    assert await AsyncEchoEmbedding().embed_batch([]) == []


async def test_declaring_batch_limit_without_implementing_it_is_refused() -> None:
    """声明批量能力不能退化成在一个闸门名额里串行发送多个请求。"""

    class Liar(EchoEmbedding):
        batch_limit = 8

    class AsyncLiar(AsyncEchoEmbedding):
        batch_limit = 8

    assert Liar().embed_batch(["one"]) == [[3.0]]
    assert await AsyncLiar().embed_batch(["one"]) == [[3.0]]
    with pytest.raises(NotImplementedError, match="batch_limit=8"):
        Liar().embed_batch(["a", "b"])
    with pytest.raises(NotImplementedError, match="batch_limit=8"):
        await AsyncLiar().embed_batch(["a", "b"])


async def test_resource_free_model_uses_noop_close() -> None:
    assert EchoEmbedding().close() is None
    assert EchoReranker().close() is None
    assert await AsyncEchoEmbedding().close() is None
    assert await AsyncEchoReranker().close() is None


def test_base_classes_satisfy_their_ports() -> None:
    """Protocol 只看形状，执行模式由静态类型与专门的协程检查保证。"""
    assert isinstance(EchoEmbedding(), EmbeddingPort)
    assert isinstance(AsyncEchoEmbedding(), AsyncEmbeddingPort)
    assert isinstance(EchoReranker(), RerankerPort)
    assert isinstance(AsyncEchoReranker(), AsyncRerankerPort)


def test_multimodal_capability_is_structural_not_advertised() -> None:
    assert not isinstance(EchoEmbedding(), MultimodalEmbeddingPort)
    assert isinstance(EchoMultimodalEmbedding(), MultimodalEmbeddingPort)
    assert not isinstance(AsyncEchoEmbedding(), AsyncMultimodalEmbeddingPort)
    assert isinstance(AsyncEchoMultimodalEmbedding(), AsyncMultimodalEmbeddingPort)
