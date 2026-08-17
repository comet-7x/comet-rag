from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.models.embedding import OpenAIEmbeddingModel


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
    assert model.embed_documents(["a", "b"]) == [[0.0], [1.0]]
    assert await model.aembed_documents(["a", "b"]) == [[0.0], [1.0]]

    assert sync_client.embeddings.calls[-1]["input"] == ["a", "b"]
    assert async_client.embeddings.calls[-1]["input"] == ["a", "b"]


async def test_native_async_batch_still_uses_process_gate() -> None:
    model, _, _ = _model()
    gate = Gate(limit=1)
    model.bind_gate(gate)

    assert await model.aembed_documents(["a", "b"]) == [[0.0], [1.0]]
    assert gate.stats.admitted == 1
