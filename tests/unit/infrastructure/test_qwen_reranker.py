from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.providers.reranker.base import BaseReranker
from comet_rag.infrastructure.providers.reranker.qwen3_vl_reranker import (
    ChatCompletionContentPartImageParam,
    ImageUrlParam,
    Qwen3VLReranker,
    ScoreMultiModalParam,
)
from comet_rag.ports import ImageContent, MediaResource, RerankDocument, TextContent


def _multimodal(url: str) -> ScoreMultiModalParam:
    return ScoreMultiModalParam(
        content=[ChatCompletionContentPartImageParam(image_url=ImageUrlParam(url=url))]
    )


@pytest.mark.parametrize("async_mode", [False, True])
async def test_remote_image_url_is_validated_before_model_request(
    async_mode: bool,
) -> None:
    requested: list[str] = []
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    def reject_private(url: str) -> None:
        validated.append(url)
        raise PermissionError("private image URL rejected")

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
        image_url_validator=reject_private,
    )
    query = _multimodal("http://127.0.0.1/metadata")
    try:
        with pytest.raises(CometRAGException, match="private image URL rejected"):
            if async_mode:
                await model.ascore(query, ["document"])
            else:
                model.score(query, ["document"])
        assert validated == ["http://127.0.0.1/metadata"]
        assert requested == []
    finally:
        sync_client.close()
        await async_client.aclose()


async def test_base64_image_does_not_require_url_policy() -> None:
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 0.5}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=client,
        image_url_validator=validated.append,
    )
    try:
        assert model.score(_multimodal("data:image/png;base64,AAAA"), ["doc"]) == [0.5]
        assert validated == []
    finally:
        await model.aclose()
        client.close()


async def test_arank_returns_sorted_documents_with_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        async_client=client,
    )
    try:
        ranked = await model.arank(
            "query",
            [
                RerankDocument(id="first", content="first text"),
                RerankDocument(id="second", content="second text"),
            ],
        )

        assert [item.document.id for item in ranked] == ["second", "first"]
        assert [item.index for item in ranked] == [1, 0]
        assert [item.score for item in ranked] == [0.9, 0.2]
    finally:
        await client.aclose()


async def test_shared_multimodal_types_are_converted_at_adapter_boundary(
    tmp_path: Path,
) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.8}]},
        )

    image = tmp_path / "cat.png"
    image.write_bytes(b"cat")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        async_client=client,
    )
    try:
        ranked = await model.arank(
            [
                TextContent("找猫"),
                ImageContent(MediaResource(path=image, mimetype="image/png")),
            ],
            [RerankDocument(id="cat", content="一只猫")],
        )

        assert ranked[0].document.id == "cat"
        query_content = payloads[0]["query"]["content"]
        assert query_content[0]["text"] == "找猫"
        assert query_content[1]["image_url"]["url"] == ("data:image/png;base64,Y2F0")
    finally:
        await client.aclose()


async def test_duplicate_result_indexes_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=client,
    )
    try:
        with pytest.raises(CometRAGException, match="重复的候选索引"):
            model.score("query", ["first", "second"])
    finally:
        await model.aclose()
        client.close()


async def test_missing_rerank_results_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.9}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=client,
    )
    try:
        with pytest.raises(CometRAGException, match="请求包含 2 个候选"):
            model.score("query", ["first", "second"])
    finally:
        await model.aclose()
        client.close()


async def test_non_contiguous_result_indexes_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.1},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=client,
    )
    try:
        with pytest.raises(CometRAGException, match="候选索引不连续"):
            model.score("query", ["first", "second"])
    finally:
        await model.aclose()
        client.close()


@pytest.mark.parametrize("async_mode", [False, True])
async def test_local_image_is_sent_as_data_url(
    async_mode: bool,
    tmp_path: Path,
) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 0.5}]}
        )

    image = tmp_path / "sample.jpeg"
    image.write_bytes(b"jpeg-bytes")
    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)
    model = Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen-rerank",
        api_key="test",
        sync_client=sync_client,
        async_client=async_client,
    )
    query = _multimodal(str(image))
    try:
        if async_mode:
            assert await model.ascore(query, ["doc"]) == [0.5]
        else:
            assert model.score(query, ["doc"]) == [0.5]

        sent_url = payloads[0]["query"]["content"][0]["image_url"]["url"]
        assert sent_url == "data:image/jpeg;base64,anBlZy1ieXRlcw=="
        original_content = query.content[0]
        assert isinstance(original_content, ChatCompletionContentPartImageParam)
        assert original_content.image_url.url == str(image)
    finally:
        sync_client.close()
        await async_client.aclose()


# ── 翻译不得堵住事件循环 ───────────────────────────────────────────────────


class _SlowTranslateReranker(BaseReranker[str]):
    """`_to_provider_input` 故意做阻塞的重活，模拟大图片的 Base64 编码。"""

    TRANSLATE_SECONDS = 0.04

    def _to_provider_input(self, content: Any) -> str:
        time.sleep(self.TRANSLATE_SECONDS)  # 同步阻塞，正是真实实现的形状
        return str(content)

    def _score(self, query: str, documents: Any, **kwargs: Any) -> list[float]:
        return [0.0] * len(list(documents))

    async def _ascore(self, query: str, documents: Any, **kwargs: Any) -> list[float]:
        return [0.0] * len(list(documents))


async def test_arank_translation_does_not_block_the_event_loop() -> None:
    """**多模态翻译要读本地文件、做 Base64，必须挪出事件循环。**

    它堵的是请求发出**之前**那一段：闸门的排队统计看不见它，监控上只表现为
    "整个进程莫名卡住"。这条用例不看耗时，只看**事件循环在此期间还能不能
    调度别的协程** —— 那才是"没堵住"的定义。
    """
    reranker = _SlowTranslateReranker()
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.002)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)  # 让心跳先跑起来
    try:
        # 1 个 query + 3 个候选 = 4 次翻译 ≈ 160ms
        await reranker.arank("q", ["a", "b", "c"])
    finally:
        beat.cancel()

    assert ticks >= 10, f"翻译期间事件循环只调度了 {ticks} 次 —— 它被堵住了"


# ── 公共入口与厂商 DTO 入口必须共用同一道准入 ──────────────────────────────


def _guarded_reranker() -> Qwen3VLReranker:
    def reject_url(reference: str) -> None:
        raise CometRAGException(f"非公网地址：{reference}")

    def reject_local(reference: str) -> None:
        raise CometRAGException(f"未开放从服务器本地路径入库：{reference}")

    return Qwen3VLReranker(
        base_url="https://model.invalid/v1",
        model_name="qwen",
        api_key="test",
        image_url_validator=reject_url,
        local_image_validator=reject_local,
    )


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (MediaResource(url="http://127.0.0.1/metadata"), "非公网地址"),
        (
            MediaResource(path=Path("/etc/passwd"), mimetype="image/png"),
            "未开放从服务器本地路径入库",
        ),
    ],
    ids=["url", "path"],
)
async def test_public_content_input_goes_through_the_same_admission_policy(
    resource: MediaResource, expected: str
) -> None:
    """**走公共 `ContentInput` 的图片，同样要过 SSRF / 本地路径准入。**

    `_to_provider_input` 把 `resource.path` / `resource.url` 原样搬进 DTO，
    读起来像绕过了校验 —— 评审就这么判过一次。实际上准入统一在
    `_prepare_inputs` → `_prepare_multimodal_content` 里做，而 `score` 与
    `_ascore` 都必经它。

    校验点只有一个是刻意的：公共入口与厂商 DTO 入口落到同一道关卡，不会出现
    "两条路两套策略"。但这条不变量此前**没有任何测试**证明 —— 那正是它看起来
    可疑的原因。现在钉住它。
    """
    reranker = _guarded_reranker()
    try:
        with pytest.raises(CometRAGException, match=expected):
            await reranker.arank(
                [ImageContent(resource)], [RerankDocument(content="d")]
            )
        with pytest.raises(CometRAGException, match=expected):
            reranker.rank([ImageContent(resource)], [RerankDocument(content="d")])
    finally:
        await reranker.aclose()
