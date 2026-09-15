"""检索基线：P50 / P95 / P99。

**为什么是分位数而不是平均值**：检索是读路径，用户感受到的是尾部。
平均 8ms、P99 400ms 的服务用起来是卡的，而只看均值完全看不出来。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.models.reranker.base import BaseReranker
from comet_rag.infrastructure.persistence.vector_store import InMemoryVectorStore
from comet_rag.ports import ExtractedDocument
from tests.e2e.test_ingest_search import (
    DIM,
    STUB_TYPE,
    KeywordEmbedding,
    _use_stub_loader,
    line_chunk_drafts,
    poll_until_terminal,
)

pytestmark = pytest.mark.benchmark

KB = "kb-search-bench"
CHUNKS = 500
#: 200 次采样：P99 至少要有两个样本落在尾部才不至于等同于 max
ROUNDS = 200
MODE_ROUNDS = 100


class IdentityReranker(BaseReranker):
    """按原顺序打分。量的是重排这条路径的**框架开销**，不是模型质量。"""

    def _score(self, query, documents, **kwargs):  # pragma: no cover
        return []

    async def _ascore(self, query, documents, **kwargs) -> list[float]:
        return [float(len(documents) - i) for i in range(len(documents))]


@pytest.fixture(scope="session")
def document(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("searchbench") / "corpus.stub"
    samples = (
        "苹果富含维生素与膳食纤维。",
        "香蕉适合在热带地区种植。",
        "橙子的酸度取决于成熟度。",
    )
    path.write_text(
        "\n".join(f"第 {i} 段：{samples[i % len(samples)]}" for i in range(CHUNKS)),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def hooks():
    @PipelineHooks.extractor(STUB_TYPE)
    def _extract(lc, config: PipelineConfig) -> ExtractedDocument:
        return ExtractedDocument(markdown=lc.path.read_text(encoding="utf-8"))

    PipelineHooks.document_chunker(STUB_TYPE)(line_chunk_drafts)

    yield


def make_config() -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(app_name="sbench", host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub-embed", dim=DIM
            ),
        ),
        limits=LimitsConfig(model_concurrency=64, max_backlog=0),
        # 这些用例走的正是"服务端读本地文件"这条**危险能力**，
        # 所以必须显式打开 —— 默认是拒绝的（PR 评审 #4）。
        ingest_policy=IngestPolicyConfig(allow_local=True),
    )


@pytest.fixture
async def client(document: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        make_config(),
        embedding_model=KeywordEmbedding(),
        reranker=IdentityReranker(),
        vector_store=InMemoryVectorStore(),
        pipeline_config=PipelineConfig(embed_batch_size=64, max_concurrency=16),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        _use_stub_loader(
            app.state.ctx,
            document,
            PipelineConfig(embed_batch_size=64, max_concurrency=16),
        )
        await http.post("/kb", json={"kb_id": KB})
        submitted = await http.post(
            "/ingest", json={"kb_id": KB, "source": "corpus.stub"}
        )
        done = await poll_until_terminal(
            http, submitted.json()["task_id"], timeout=120.0
        )
        assert done["status"] == "succeeded", done
        yield http


async def _search(
    http: httpx.AsyncClient,
    *,
    rerank: bool,
    top_k: int = 5,
    mode: str = "dense",
) -> dict[str, Any]:
    resp = await http.post(
        "/search",
        json={
            "kb_id": KB,
            "query": "香蕉",
            "top_k": top_k,
            "rerank": rerank,
            "mode": mode,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunks"]
    return body


async def test_search_latency_without_rerank(client: httpx.AsyncClient, bench) -> None:
    """纯向量召回。这是降级到 L1 之后用户实际体验到的那条路径。"""
    await bench.measure(
        ROUNDS, lambda i: _search(client, rerank=False), metric="search_no_rerank"
    )


async def test_search_latency_with_rerank(client: httpx.AsyncClient, bench) -> None:
    """带重排。与上一条的差值就是 rerank 在框架侧的成本 ——
    降级时先砍它，正是因为这一段最贵（真实模型下差距只会更大）。"""
    await bench.measure(
        ROUNDS, lambda i: _search(client, rerank=True), metric="search_rerank"
    )


async def test_search_latency_scales_with_top_k(
    client: httpx.AsyncClient, bench
) -> None:
    """top_k 从 5 提到 50。降级 L2 砍 top_k 的收益就体现在这条曲线上。"""
    await bench.measure(
        ROUNDS // 2,
        lambda i: _search(client, rerank=True, top_k=50),
        metric="search_top_k_50",
    )


async def test_search_modes_record_fixed_sample_signal(
    client: httpx.AsyncClient, bench
) -> None:
    """固定合成语料只用于回归观测，不据此宣称真实检索质量提升。"""
    for mode in ("dense", "keyword", "hybrid"):
        await bench.measure(
            MODE_ROUNDS,
            lambda i, mode=mode: _search(client, rerank=False, mode=mode),
            metric=f"search_{mode}",
        )
        body = await _search(client, rerank=False, mode=mode)
        hit = float("香蕉" in body["chunks"][0]["text"])
        bench.record(f"{mode}_hit_at_1", hit, "ratio")
        bench.record(f"{mode}_candidates", float(body["fetched"]), "chunks")

        assert hit == 1.0
        assert body["mode"] == mode
