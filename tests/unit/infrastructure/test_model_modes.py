from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.models.embedding import (
    AsyncOpenAIEmbeddingModel,
    OpenAIEmbeddingModel,
)


@dataclass(frozen=True)
class ModelPair:
    module: str
    name: str
    client_name: str
    request_methods: tuple[str, ...]

    def source(self) -> ModuleType:
        return import_module(f"comet_rag.infrastructure.models.{self.module}")

    def model_type(self, async_mode: bool) -> type[Any]:
        # 参数化供应商与执行模式；具体公开签名由下方的执行模式断言覆盖。
        return getattr(self.source(), ("Async" if async_mode else "") + self.name)


PAIRS = [
    ModelPair(
        "embedding.openai",
        "OpenAIEmbeddingModel",
        "OpenAI",
        ("embed", "embed_query", "embed_document", "embed_batch"),
    ),
    ModelPair(
        "embedding.qwen3_vl",
        "Qwen3VLEmbeddingModel",
        "Client",
        (
            "embed",
            "embed_query",
            "embed_document",
            "embed_batch",
            "embed_media",
            "embed_image",
            "embed_content",
            "tokenize",
            "detokenize",
            "get_output_dim",
            "get_max_model_len",
        ),
    ),
    ModelPair("reranker.qwen3_vl", "Qwen3VLReranker", "Client", ("score", "rank")),
    ModelPair("vision.openai", "OpenAIVisionModel", "OpenAI", ("describe",)),
]
CONFIG = {
    "base_url": "https://model.invalid/v1",
    "model_name": "test",
    "api_key": "test",
}


@pytest.mark.parametrize("pair", PAIRS, ids=lambda pair: pair.name)
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_model_requests_and_close_have_one_execution_mode(
    pair: ModelPair, async_mode: bool
) -> None:
    model_type = pair.model_type(async_mode)
    for name in (*pair.request_methods, "close"):
        assert inspect.iscoroutinefunction(getattr(model_type, name)) is async_mode
        assert not hasattr(model_type, f"a{name}"), f"旧入口 a{name} 不应继续存在"


class _SyncClient:
    def __init__(self, closed: list[str], fail: bool = False) -> None:
        self.closed = closed
        self.fail = fail

    def close(self) -> None:
        self.closed.append("sync")
        if self.fail:
            raise RuntimeError("close failed")


class _AsyncClient:
    def __init__(self, closed: list[str], fail: bool = False) -> None:
        self.closed = closed
        self.fail = fail

    async def close(self) -> None:
        self.closed.append("async")
        if self.fail:
            raise RuntimeError("close failed")

    async def aclose(self) -> None:
        # 上游 httpx 与 OpenAI 的关闭方法名称不同。
        await self.close()


@pytest.mark.parametrize("pair", PAIRS, ids=lambda pair: pair.name)
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("fail_close", [False, True], ids=["close-ok", "close-fails"])
async def test_each_model_constructs_and_closes_only_its_own_client(
    pair: ModelPair, async_mode: bool, fail_close: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed: list[str] = []
    closed: list[str] = []
    client = (
        _AsyncClient(closed, fail_close)
        if async_mode
        else _SyncClient(closed, fail_close)
    )

    def factory(**kwargs: object) -> object:
        constructed.append("own")
        return client

    def wrong_factory(**kwargs: object) -> object:
        pytest.fail("单模式模型不应创建另一执行模式的客户端")

    own = ("Async" if async_mode else "") + pair.client_name
    other = ("" if async_mode else "Async") + pair.client_name
    monkeypatch.setattr(pair.source(), own, factory)
    monkeypatch.setattr(pair.source(), other, wrong_factory)
    model = pair.model_type(async_mode)(**CONFIG)
    assert model.client is client
    assert constructed == ["own"]
    assert not hasattr(model, "sync_client")
    assert not hasattr(model, "async_client")

    async def close() -> None:
        if async_mode:
            await model.close()
        else:
            assert model.close() is None

    if fail_close:
        with pytest.raises(RuntimeError, match="close failed"):
            await close()
    else:
        await close()
    assert closed == ["async" if async_mode else "sync"]


@pytest.mark.parametrize("pair", PAIRS, ids=lambda pair: pair.name)
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
async def test_borrowed_client_remains_owned_by_caller(
    pair: ModelPair, async_mode: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []
    client = _AsyncClient(closed) if async_mode else _SyncClient(closed)

    def refuse_creation(**kwargs: object) -> object:
        pytest.fail("注入客户端后不应创建任何备用客户端")

    monkeypatch.setattr(pair.source(), pair.client_name, refuse_creation)
    monkeypatch.setattr(pair.source(), f"Async{pair.client_name}", refuse_creation)
    model = pair.model_type(async_mode)(**CONFIG, client=client)
    if async_mode:
        await model.close()
    else:
        assert model.close() is None
    assert closed == []


@pytest.mark.parametrize("pair", PAIRS, ids=lambda pair: pair.name)
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_constructor_failure_has_no_second_client_to_roll_back(
    pair: ModelPair, async_mode: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fail(**kwargs: object) -> object:
        calls.append("own")
        raise RuntimeError("constructor failed")

    def wrong_factory(**kwargs: object) -> object:
        pytest.fail("不能为了回滚而提前创建另一模式的客户端")

    monkeypatch.setattr(
        pair.source(), ("Async" if async_mode else "") + pair.client_name, fail
    )
    monkeypatch.setattr(
        pair.source(), ("" if async_mode else "Async") + pair.client_name, wrong_factory
    )
    with pytest.raises(RuntimeError, match="constructor failed"):
        pair.model_type(async_mode)(**CONFIG)
    assert calls == ["own"]


def _response(path: str) -> dict[str, object]:
    if path.endswith("/chat/completions"):
        return {
            "id": "chat-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "a cat"},
                }
            ],
        }
    if path.endswith("/rerank"):
        return {"results": [{"index": 0, "relevance_score": 0.8}]}
    return {
        "id": "embedding-1",
        "object": "list",
        "created": 0,
        "model": "test",
        "data": [{"index": 0, "object": "embedding", "embedding": [1.0]}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


@pytest.mark.parametrize("pair", PAIRS, ids=lambda pair: pair.name)
async def test_async_adapters_use_native_async_network_on_the_calling_loop(
    pair: ModelPair,
) -> None:
    loop_thread = threading.get_ident()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert threading.get_ident() == loop_thread
        await asyncio.sleep(0)
        calls.append(request.url.path)
        return httpx.Response(200, json=_response(request.url.path))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(base_url=CONFIG["base_url"], api_key="test", http_client=http)
        model = pair.model_type(True)(
            **CONFIG, client=sdk if pair.client_name == "OpenAI" else http
        )
        if pair.module.startswith("embedding"):
            assert await model.embed_query("text") == [1.0]
        elif pair.module.startswith("reranker"):
            assert (await model.rank("q", ["doc"]))[0].score == 0.8
        else:
            assert await model.describe("Y2F0", "image/png") == "a cat"
        await model.close()
        assert not http.is_closed, "外部注入的 SDK/HTTP 客户端不归模型所有"
    assert len(calls) == 1


async def test_sync_and_async_instances_share_one_injected_budget() -> None:
    started = threading.Event()
    release = threading.Event()
    async_started = False

    def sync_create(**kwargs: object) -> SimpleNamespace:
        started.set()
        assert release.wait(2), "测试未释放同步请求"
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0])])

    async def async_create(**kwargs: object) -> SimpleNamespace:
        nonlocal async_started
        async_started = True
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[2.0])])

    # 只替代 SDK 的请求边界，模板、闸门与结果转换都运行真实实现。
    sync_client: Any = SimpleNamespace(embeddings=SimpleNamespace(create=sync_create))
    async_client: Any = SimpleNamespace(embeddings=SimpleNamespace(create=async_create))
    model = OpenAIEmbeddingModel(
        "https://model.invalid/v1", "test", "test", client=sync_client
    )
    async_model = AsyncOpenAIEmbeddingModel(
        "https://model.invalid/v1", "test", "test", client=async_client
    )
    gate = Gate(limit=1, max_waiting=1)
    model.bind_gate(gate)
    async_model.bind_gate(gate)
    sync_call = asyncio.create_task(asyncio.to_thread(model.embed_query, "q"))
    async_call: asyncio.Task[list[float]] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        async_call = asyncio.create_task(async_model.embed_document("doc"))
        async with asyncio.timeout(1):
            while gate.stats.waiting != 1:
                await asyncio.sleep(0)
        assert not async_started
    finally:
        release.set()
        await sync_call
        if async_call is not None:
            await async_call
    assert async_started
    assert gate.stats.admitted == 2
    assert gate.stats.peak_in_flight == 1
    assert gate.stats.in_flight == 0


@pytest.mark.parametrize("pair", PAIRS[:3], ids=lambda pair: pair.name)
async def test_cancelled_native_request_propagates_and_releases_gate(
    pair: ModelPair,
) -> None:
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("取消后的请求不应继续")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(base_url=CONFIG["base_url"], api_key="test", http_client=http)
        model = pair.model_type(True)(
            **CONFIG, client=sdk if pair.client_name == "OpenAI" else http
        )
        gate = Gate(limit=1)
        model.bind_gate(gate)
        request = (
            model.embed_query("text")
            if pair.module.startswith("embedding")
            else model.rank("q", ["doc"])
        )
        task = asyncio.create_task(request)
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.stats.in_flight == 0


@pytest.mark.parametrize("family", ["embedding", "reranker"])
@pytest.mark.parametrize("input_kind", ["path", "url"])
async def test_media_preparation_finishes_before_cancellation_returns(
    family: str, input_kind: str, tmp_path: Path
) -> None:
    from comet_rag.infrastructure.models import (  # noqa: PLC0415
        AsyncQwen3VLEmbeddingModel,
        AsyncQwen3VLReranker,
    )
    from comet_rag.ports import ImageContent, MediaResource  # noqa: PLC0415

    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    started = threading.Event()
    release = threading.Event()
    prepared = threading.Event()
    requested: list[str] = []

    def validate_media(reference: str) -> None:
        started.set()
        assert release.wait(2), "测试未释放媒体准备线程"
        if input_kind == "path":
            assert image.read_bytes() == b"image", "调用返回前输入文件必须仍可读取"
        else:
            assert reference == "https://images.example.test/cat.png"
        prepared.set()

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resource = (
            MediaResource(path=image, mimetype="image/png")
            if input_kind == "path"
            else MediaResource(url="https://images.example.test/cat.png")
        )
        if family == "embedding":
            model = AsyncQwen3VLEmbeddingModel(
                "https://model.invalid/v1",
                "qwen",
                "test",
                client=client,
                local_image_validator=validate_media,
                image_url_validator=validate_media,
            )
            request = model.embed_image(resource)
        else:
            reranker = AsyncQwen3VLReranker(
                "https://model.invalid/v1",
                "qwen",
                "test",
                client=client,
                local_image_validator=validate_media,
                image_url_validator=validate_media,
            )
            request = reranker.rank([ImageContent(resource)], ["document"])
        task = asyncio.create_task(request)
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "取消不能让调用方提前清理线程仍在使用的文件"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert prepared.is_set()
        assert requested == [], "准备期间取消后不应再向模型发送请求"
