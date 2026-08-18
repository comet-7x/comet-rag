from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

from comet_rag.core.concurrency import Gate
from comet_rag.engines.embedding.batch import aembed_documents, embed_documents
from comet_rag.infrastructure.providers.embedding import OpenAIEmbeddingModel


@dataclass
class _Embedding:
    index: int
    embedding: list[float]


class _SyncEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        values = kwargs["input"]
        values = [values] if isinstance(values, str) else values
        # 故意逆序返回，适配器必须依据 index 恢复输入顺序。
        data = [
            _Embedding(index=index, embedding=[float(index)])
            for index in reversed(range(len(values)))
        ]
        return SimpleNamespace(data=data)


class _AsyncEmbeddings(_SyncEmbeddings):
    async def create(self, **kwargs: Any) -> Any:
        return super().create(**kwargs)


class _SyncClient:
    def __init__(self) -> None:
        self.embeddings = _SyncEmbeddings()


class _AsyncClient:
    def __init__(self) -> None:
        self.embeddings = _AsyncEmbeddings()


def _model() -> tuple[OpenAIEmbeddingModel, _SyncClient, _AsyncClient]:
    sync_client = _SyncClient()
    async_client = _AsyncClient()
    model = OpenAIEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="text-embedding",
        api_key="test",
        sync_client=cast(Any, sync_client),
        async_client=cast(Any, async_client),
    )
    return model, sync_client, async_client


async def test_query_and_document_interfaces_are_immediately_usable() -> None:
    model, sync_client, async_client = _model()

    assert model.embed_query("query") == [0.0]
    assert await model.aembed_query("query") == [0.0]
    assert embed_documents(model, ["a", "b"]) == [[0.0], [1.0]]
    assert await aembed_documents(model, ["a", "b"]) == [[0.0], [1.0]]

    assert sync_client.embeddings.calls[-1]["input"] == ["a", "b"]
    assert async_client.embeddings.calls[-1]["input"] == ["a", "b"]


async def test_native_async_batch_still_uses_process_gate() -> None:
    """原生批量是一个请求，所以只占**一个**闸门名额 —— 装 1 篇还是 512 篇都一样。"""
    model, _, _ = _model()
    gate = Gate(limit=1)
    model.bind_gate(gate)

    assert await model.aembed_batch(["a", "b"]) == [[0.0], [1.0]]
    assert gate.stats.admitted == 1


async def test_explicit_base64_encoding_is_decoded_to_floats() -> None:
    """**显式传 `encoding_format="base64"` 不能让契约破掉。**

    OpenAI SDK 只在调用方没指定该参数时才自动解码；显式传 `"base64"`（经
    `**kwargs` 透传到 `embeddings.create`）时拿到的是字符串。若原样返回，
    `embed_query` 声明 `list[float]`、实际给出 `str` —— 与 Qwen 适配器先前
    同病，只是这一处直到评审才被发现。
    """
    model, _, _ = _model()
    # 0.25, -0.5, 1.5 的小端 float32
    payload = base64.b64encode(struct.pack("<3f", 0.25, -0.5, 1.5)).decode()

    class _Item:
        index = 0
        embedding = payload

    class _Resp:
        data = [_Item()]

    assert model._vectors(_Resp(), expected_count=1) == [[0.25, -0.5, 1.5]]
