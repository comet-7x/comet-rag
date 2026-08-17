from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.embedding.qwen3_vl_embedding import (
    EmbeddingData,
    EncodingFormat,
    Qwen3VLEmbeddingModel,
)
from comet_rag.models import ImageContent, MediaResource, TextContent


async def test_qwen_adapter_accepts_base_text_contract() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": [1.0, 2.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    try:
        assert await model.aembed("plain service text") == [1.0, 2.0]
        assert payloads[0]["messages"][1]["content"][0]["text"] == (
            "plain service text"
        )
    finally:
        sync_client.close()
        await async_client.aclose()


async def test_semantic_embedding_methods_choose_query_and_document_prompts() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": [1.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    try:
        await model.aembed_query("猫在哪里")
        await model.aembed_documents(["猫在沙发上"])

        assert payloads[0]["messages"][0]["content"][0]["text"] == (
            "Represent the query for retrieval."
        )
        assert payloads[1]["messages"][0]["content"][0]["text"] == (
            "Represent the document for retrieval."
        )
    finally:
        sync_client.close()
        await async_client.aclose()


async def test_public_multimodal_content_accepts_local_media_resource(
    tmp_path: Path,
) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": [1.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    image = tmp_path / "cat.png"
    image.write_bytes(b"cat")
    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    try:
        result = await model.aembed_content(
            [
                TextContent("一只猫"),
                ImageContent(MediaResource(path=image, mimetype="image/png")),
            ]
        )

        assert result == [1.0]
        content = payloads[0]["messages"][1]["content"]
        assert content[0]["image_url"]["url"] == "data:image/png;base64,Y2F0"
        assert content[1]["text"] == "一只猫"
    finally:
        sync_client.close()
        await async_client.aclose()


@pytest.mark.parametrize("operation", ["embed", "aembed", "tokenize", "atokenize"])
async def test_remote_image_url_is_validated_before_model_request(
    operation: str,
) -> None:
    requested: list[str] = []
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(500)

    def reject_private(url: str) -> None:
        validated.append(url)
        raise PermissionError("private image URL rejected")

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
        image_url_validator=reject_private,
    )
    data = EmbeddingData(image_url="http://127.0.0.1/metadata")
    # 走公开的多模态入口而不是厂商 DTO：业务代码拿得到的就是 MediaResource，
    # 准入策略必须在**那条路**上生效才算数。
    media = MediaResource(url="http://127.0.0.1/metadata")
    try:
        with pytest.raises(CometRAGException, match="private image URL rejected"):
            if operation == "embed":
                model.embed_media(media)
            elif operation == "aembed":
                await model.aembed_media(media)
            elif operation == "tokenize":
                model.tokenize(data)
            else:
                await model.atokenize(data)

        assert validated == ["http://127.0.0.1/metadata"]
        assert requested == []
    finally:
        sync_client.close()
        await async_client.aclose()


async def test_empty_embedding_response_is_reported_as_adapter_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=client,
    )
    try:
        with pytest.raises(CometRAGException, match="data"):
            model.embed("text")
    finally:
        await model.aclose()
        client.close()


@pytest.mark.parametrize("operation", ["embed", "aembed", "tokenize", "atokenize"])
async def test_local_image_is_sent_as_data_url(
    operation: str,
    tmp_path: Path,
) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if request.url.path.endswith("/tokenize"):
            return httpx.Response(
                200,
                json={"count": 1, "max_model_len": 1024, "tokens": [1]},
            )
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": [1.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    image = tmp_path / "sample.png"
    image.write_bytes(b"png-bytes")
    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    data = EmbeddingData(image_url=str(image))
    media = MediaResource(path=image, mimetype="image/png")
    try:
        if operation == "embed":
            model.embed_media(media)
        elif operation == "aembed":
            await model.aembed_media(media)
        elif operation == "tokenize":
            model.tokenize(data)
        else:
            await model.atokenize(data)

        sent_url = payloads[0]["messages"][1]["content"][0]["image_url"]["url"]
        assert sent_url == "data:image/png;base64,cG5nLWJ5dGVz"
        assert data.image_url == str(image), "适配器不应修改调用方持有的输入模型"
    finally:
        sync_client.close()
        await async_client.aclose()


def test_local_image_size_limit_is_enforced(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(500)

    image = tmp_path / "oversized.png"
    image.write_bytes(b"1234")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=client,
        max_local_image_bytes=3,
    )
    try:
        with pytest.raises(CometRAGException, match="图片大小超过上限 3 bytes"):
            model.embed(EmbeddingData(image_url=str(image)))
        assert requested == []
    finally:
        client.close()


async def test_continue_final_message_controls_assistant_placeholder() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if request.url.path.endswith("/tokenize"):
            return httpx.Response(
                200,
                json={"count": 1, "max_model_len": 1024, "tokens": [1]},
            )
        return httpx.Response(
            200,
            json={
                "id": "embedding-1",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": [1.0]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    data = EmbeddingData(text="plain text")
    try:
        model.embed("plain text", continue_final_message=False)
        await model.aembed("plain text", continue_final_message=False)
        model.tokenize(data, continue_final_message=False)
        await model.atokenize(data, continue_final_message=False)
        model.embed(data, continue_final_message=True)

        assert all(
            payload["messages"][-1]["role"] == "user" for payload in payloads[:4]
        )
        assert payloads[4]["messages"][-1]["role"] == "assistant"
    finally:
        sync_client.close()
        await async_client.aclose()


async def test_base64_embeddings_are_decoded_to_floats() -> None:
    """`encoding_format=base64` 是**传输优化**，不该泄漏给调用方。

    服务端按 OpenAI 协议返回小端 float32 的 base64 串。若原样返回，
    `embed_query` 声明 `list[float]` 却给出 `str` —— 契约在说谎，而且只在
    配了 base64 的部署上才炸，本地怎么测都测不出来。
    """
    import base64 as _b64  # noqa: PLC0415
    import struct as _struct  # noqa: PLC0415

    vector = [0.25, -0.5, 1.5]
    packed = _b64.b64encode(_struct.pack(f"<{len(vector)}f", *vector)).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "embedding-b64",
                "object": "list",
                "created": 0,
                "model": "qwen",
                "data": [{"index": 0, "object": "embedding", "embedding": packed}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLEmbeddingModel(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    try:
        assert model.embed("x", encoding_format=EncodingFormat.BASE64) == vector
        assert await model.aembed("x", encoding_format=EncodingFormat.BASE64) == vector
    finally:
        sync_client.close()
        await async_client.aclose()
