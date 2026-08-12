"""S4-5 的验收用例：**注入模型服务超时，检索仍返回结果（无 rerank），
且日志有降级记录。**

这条链路只在装配全对的时候才通：闸门要挂上（否则收集不到失败）、观测者要
接到降级控制器（否则失败率永远是 0）、检索要问过控制器（否则降级了也不生效）。
少任何一环，本用例都会红 —— 这正是它比单测更有价值的地方。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from comet_rag.api.main import create_app
from comet_rag.config.schemas import (
    APPConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    IngestPolicyConfig,
    LimitsConfig,
    ServerConfig,
)
from comet_rag.core.degradation import Level
from comet_rag.core.logging import logger
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.models.reranker.base import BaseReranker
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from tests.e2e.test_ingest_search import (
    DIM,
    STUB_TYPE,
    KeywordEmbedding,
    _use_stub_loader,
    poll_until_terminal,
)

pytestmark = pytest.mark.e2e

KB = "kb-degrade"


class FlakyReranker(BaseReranker):
    """可以被切成"总是超时"的重排。超时是模型服务过载最典型的表现。"""

    def __init__(self) -> None:
        self.failing = False
        self.calls = 0

    def score(self, query, documents, **kwargs):  # pragma: no cover
        return []

    async def _ascore(self, query, documents, **kwargs) -> list[float]:
        self.calls += 1
        if self.failing:
            raise TimeoutError("模型服务超时")
        # 正常时把顺序倒过来，好让"重排确实生效了"看得出来
        return [float(i) for i in range(len(documents))]


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "fruits.stub"
    path.write_text(
        "苹果富含维生素与膳食纤维。\n香蕉适合在热带地区种植。\n橙子的酸度取决于成熟度。",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def hooks():
    @PipelineHooks.extractor(STUB_TYPE)
    def _extract(lc, config: PipelineConfig) -> str:
        return lc.path.read_text(encoding="utf-8")

    @PipelineHooks.chunker(STUB_TYPE)
    def _chunk(text: str, config: PipelineConfig) -> list[str]:
        return [line for line in text.splitlines() if line.strip()]

    yield


def make_config() -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(app_name="degrade", host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub-embed", dim=DIM
            ),
        ),
        # 阈值压低、样本要求压小：本用例要验的是链路，不是阈值本身
        limits=LimitsConfig(
            degrade_failure_rate=(0.2, 0.9, 0.99),
            degrade_recover_after=1.0,
            degrade_min_samples=5,
        ),
        # 这些用例走的正是"服务端读本地文件"这条**危险能力**，
        # 所以必须显式打开 —— 默认是拒绝的（PR 评审 #4）。
        ingest_policy=IngestPolicyConfig(allow_local=True),
    )


@pytest.fixture
async def app_and_client(
    document: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, FlakyReranker]]:
    reranker = FlakyReranker()
    app = create_app(
        make_config(),
        embedding_model=KeywordEmbedding(),
        reranker=reranker,
        vector_store=InMemoryVectorStore(),
        pipeline_config=PipelineConfig(embed_batch_size=2, max_concurrency=4),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        _use_stub_loader(app.state.ctx, document)
        yield http, reranker


async def _ingest(client: httpx.AsyncClient) -> None:
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    done = await poll_until_terminal(client, submitted.json()["task_id"], timeout=10.0)
    assert done["status"] == "succeeded", done


async def test_model_timeouts_degrade_rerank_but_search_still_answers(
    app_and_client: tuple[httpx.AsyncClient, FlakyReranker],
) -> None:
    """**S4-5 的原文验收**：注入超时 → 检索仍有结果、无 rerank、日志有记录。"""
    client, reranker = app_and_client
    await _ingest(client)

    # 正常时重排确实在跑
    ok = await client.post("/search", json={"kb_id": KB, "query": "苹果", "top_k": 3})
    assert ok.status_code == 200
    assert ok.json()["reranked"] is True
    calls_before = reranker.calls

    # 注入超时，并收集降级日志
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    reranker.failing = True
    try:
        for _ in range(10):
            resp = await client.post(
                "/search", json={"kb_id": KB, "query": "苹果", "top_k": 3}
            )
            assert resp.status_code == 200, "降级期间检索必须仍然可用，不能失败"
            assert resp.json()["chunks"], "降级了也得给出向量召回的结果"
    finally:
        logger.remove(sink_id)

    ctx = client._transport.app.state.ctx  # noqa: SLF001
    assert ctx.degradation.level() >= Level.NO_RERANK, (
        f"超时打了 10 轮却没降级：{ctx.degradation.stats}"
    )
    assert any("服务降级" in r for r in records), f"降级没留下日志：{records}"

    # 降级之后不该再去打那个坏掉的重排 —— 否则等于白降
    last = await client.post("/search", json={"kb_id": KB, "query": "苹果", "top_k": 3})
    assert last.json()["reranked"] is False
    stalled = reranker.calls
    await client.post("/search", json={"kb_id": KB, "query": "苹果", "top_k": 3})
    assert reranker.calls == stalled, "降级后仍在调用坏掉的重排"

    assert calls_before > 0


async def test_admin_limits_exposes_the_degradation_state(
    app_and_client: tuple[httpx.AsyncClient, FlakyReranker],
) -> None:
    """降级是"服务悄悄变差"，必须能从外部看到，否则排查时无从下手。"""
    client, reranker = app_and_client
    await _ingest(client)

    reranker.failing = True
    for _ in range(10):
        await client.post("/search", json={"kb_id": KB, "query": "苹果"})

    body = (await client.get("/admin/limits")).json()
    assert body["degradation"]["level"] != "NORMAL", body
    assert body["degradation"]["failure_rate"] > 0
    assert body["model_gate"]["limit"] > 0
    assert "pending" in body["backlog"]


async def test_degraded_top_k_is_surfaced_to_the_client(
    app_and_client: tuple[httpx.AsyncClient, FlakyReranker],
) -> None:
    """**降级后的 top_k 必须告诉客户端**（PR 评审 #12）。

    不暴露的话，客户端分不清"结果少是因为库里就这么多"还是"服务在降级
    运行"—— 前者该改查询，后者该等一等或扩容，处理方式完全相反。
    """
    client, reranker = app_and_client
    await _ingest(client)

    normal = (
        await client.post("/search", json={"kb_id": KB, "query": "苹果", "top_k": 3})
    ).json()
    assert normal["effective_top_k"] == 3
    assert normal["degraded"] is None, "正常时不该报降级"

    # 打到 L2（砍 top_k）：本用例配的第二档阈值是 90%，而窗口里还留着前面
    # 那些成功的调用 —— 灌 20 条只到 80%，够不着。灌满一窗口才稳。
    ctx = client._transport.app.state.ctx  # noqa: SLF001
    for _ in range(200):
        ctx.degradation.record(False)

    degraded = (
        await client.post("/search", json={"kb_id": KB, "query": "苹果", "top_k": 3})
    ).json()

    assert degraded["degraded"] is not None, "降级了却没在响应里说"
    assert degraded["effective_top_k"] < 3, (
        f"L2 应当砍 top_k，实际 effective_top_k={degraded['effective_top_k']}"
    )
    assert len(degraded["chunks"]) <= degraded["effective_top_k"]
