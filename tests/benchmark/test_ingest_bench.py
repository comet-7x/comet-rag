"""入库基线：单文档耗时、批量吞吐、以及"并发有没有真的重叠"。

前两条量的是**框架开销**（替身模型瞬间返回），第三条故意给替身加了固定延迟，
量的是并发行为本身 —— 详见 `conftest.py` 顶部。
"""

from __future__ import annotations

import asyncio
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
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from tests.e2e.test_ingest_search import (
    DIM,
    STUB_TYPE,
    KeywordEmbedding,
    _use_stub_loader,
    poll_until_terminal,
)

pytestmark = pytest.mark.benchmark

KB = "kb-bench"
#: 一份"中等大小"的文档：200 段，与 spec S4-3 讨论的规模一致
CHUNKS = 200
PIPELINE = PipelineConfig(embed_batch_size=32, max_concurrency=16)


@pytest.fixture(scope="session")
def document(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("bench") / "doc.stub"
    path.write_text(
        "\n".join(f"第 {i} 段：苹果香蕉橙子的相关描述文字。" for i in range(CHUNKS)),
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


class DelayedEmbedding(KeywordEmbedding):
    """固定延迟的替身。用来把"并发有没有重叠"变成可测的量。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def _aembed(self, data, **kwargs):
        await asyncio.sleep(self.delay)
        return await super()._aembed(data, **kwargs)


def make_config(concurrency: int = 64) -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(app_name="bench", host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub-embed", dim=DIM
            ),
        ),
        # 闸门开得比扇出宽：本文件量的是框架与并发，不是限流
        limits=LimitsConfig(model_concurrency=concurrency, max_backlog=0),
        # 这些用例走的正是"服务端读本地文件"这条**危险能力**，
        # 所以必须显式打开 —— 默认是拒绝的（PR 评审 #4）。
        ingest_policy=IngestPolicyConfig(allow_local=True),
    )


async def _make_client(
    document: Path, embedding=None, concurrency: int = 64
) -> tuple[httpx.AsyncClient, Any]:  # noqa: ANN401
    app = create_app(
        make_config(concurrency),
        embedding_model=embedding or KeywordEmbedding(),
        vector_store=InMemoryVectorStore(),
        pipeline_config=PIPELINE,
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), app


@pytest.fixture
async def client(document: Path) -> AsyncIterator[httpx.AsyncClient]:
    http, app = await _make_client(document)
    async with http, app.router.lifespan_context(app):
        _use_stub_loader(app.state.ctx, document, PIPELINE)
        await http.post("/kb", json={"kb_id": KB})
        yield http


async def _ingest_once(http: httpx.AsyncClient, tag: int) -> None:
    submitted = await http.post(
        "/ingest",
        json={"kb_id": KB, "source": "doc.stub", "idempotency_key": f"bench-{tag}"},
    )
    assert submitted.status_code == 202, submitted.text
    done = await poll_until_terminal(http, submitted.json()["task_id"], timeout=60.0)
    assert done["status"] == "succeeded", done


async def test_single_document_ingest_latency(client: httpx.AsyncClient, bench) -> None:
    """单文档（200 段）从提交到 SUCCEEDED 的端到端耗时。

    包含轮询间隔在内 —— 那也是用户真实感受到的一部分。
    """
    await bench.measure(10, lambda i: _ingest_once(client, i), metric="ingest_e2e")


async def test_ingest_throughput(client: httpx.AsyncClient, bench) -> None:
    """并发投 20 份文档，量端到端吞吐（文档/秒、段/秒）。"""
    import time  # noqa: PLC0415

    docs = 20
    start = time.perf_counter()
    await asyncio.gather(*(_ingest_once(client, 1000 + i) for i in range(docs)))
    elapsed = time.perf_counter() - start

    bench.record("throughput_docs", docs / elapsed, "doc/s", elapsed_s=elapsed)
    bench.record("throughput_chunks", docs * CHUNKS / elapsed, "chunk/s")


async def test_embedding_overlaps_io(document: Path, bench) -> None:
    """**这条是可以断言的**：并发必须真的重叠（spec S4-3）。

    给替身加 5ms 固定延迟。200 段若逐条串行，光等待就是 1 秒；
    窗口化并发下应当低得多。判据是**数量级**而非具体毫秒数，
    所以换机器也不会假红。

    T9 修好的正是这件事（修复前 `astream_run` 并发峰值恒为 1）。
    没有这条，那次修复会在某天被"顺手改回逐条 await"而无人发现。
    """
    delay = 0.005
    serial_ms = CHUNKS * delay * 1000  # 完全串行的下界

    http, app = await _make_client(
        document, embedding=DelayedEmbedding(delay), concurrency=64
    )
    async with http, app.router.lifespan_context(app):
        _use_stub_loader(app.state.ctx, document, PIPELINE)
        await http.post("/kb", json={"kb_id": KB})
        samples = await bench.measure(
            3, lambda i: _ingest_once(http, 2000 + i), metric="ingest_with_5ms_model"
        )

    best = min(samples)
    bench.record("overlap_speedup", serial_ms / best, "x", serial_ms=serial_ms)
    assert best < serial_ms / 4, (
        f"200 段 × 5ms 串行需 {serial_ms:.0f}ms，实测最快 {best:.0f}ms —— "
        f"并发没有真的重叠，多半是哪里退回了逐条 await（spec S4-3）"
    )
