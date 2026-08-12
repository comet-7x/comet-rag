"""有界背压：投递量远超处理能力时，服务**明确拒绝**而不是无限堆积（S4-1）。

spec 的验证条款是"投递量 10× 于处理能力，进程存活且内存不随投递量线性增长"。
内存本身不好断言（GC 时机、解释器缓存都会干扰），所以这里断言的是**因**而非果：
待执行任务数**始终**不超过配置的上限。上限守住了，内存自然就有界 ——
反过来只测内存，测出来的多半是运行环境的噪声。

拒绝必须是 **429**：静默排队或 500 都会让客户端继续加压，
恰好是过载时最不该发生的事。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from comet_rag.api.main import create_app
from comet_rag.config.schemas import (
    APPConfig,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    IngestPolicyConfig,
    LimitsConfig,
    ServerConfig,
)
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from comet_rag.tasks import TaskStatus
from tests.e2e.test_ingest_search import (
    DIM,
    STUB_TYPE,
    KeywordEmbedding,
    _use_stub_loader,
)

pytestmark = pytest.mark.e2e

KB = "kb-backpressure"
#: 压得很小，好让"超限"在几十次投递内就发生，而不是靠灌几千条
MAX_BACKLOG = 5


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "slow.stub"
    path.write_text("一段文本。\n另一段文本。", encoding="utf-8")
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


class SlowEmbedding(KeywordEmbedding):
    """慢到足以让任务堆起来 —— 处理能力远低于投递速度，正是要验的场景。"""

    async def _aembed(self, data, **kwargs):
        await asyncio.sleep(0.05)
        return await super()._aembed(data, **kwargs)


def make_config() -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(app_name="bp", host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub-embed", dim=DIM
            ),
        ),
        backends=BackendsConfig(max_concurrency=2),
        limits=LimitsConfig(
            model_concurrency=2,
            model_queue=8,
            model_wait_timeout=5.0,
            max_backlog=MAX_BACKLOG,
        ),
        # 这些用例走的正是"服务端读本地文件"这条**危险能力**，
        # 所以必须显式打开 —— 默认是拒绝的（PR 评审 #4）。
        ingest_policy=IngestPolicyConfig(allow_local=True),
    )


@pytest.fixture
async def client(document: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        make_config(),
        embedding_model=SlowEmbedding(),
        vector_store=InMemoryVectorStore(),
        pipeline_config=PipelineConfig(embed_batch_size=2, max_concurrency=4),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        _use_stub_loader(app.state.ctx, document)
        yield http


async def test_overload_is_refused_with_429_not_queued_forever(
    client: httpx.AsyncClient,
) -> None:
    """投 10× 于处理能力，服务活着、拒绝清晰、积压始终有界。"""
    await client.post("/kb", json={"kb_id": KB})
    ctx = client._transport.app.state.ctx  # noqa: SLF001 —— 只为读积压，不改状态

    accepted = refused = 0
    peak_backlog = 0
    for i in range(MAX_BACKLOG * 10):
        resp = await client.post(
            "/ingest",
            json={"kb_id": KB, "source": "slow.stub", "idempotency_key": f"doc-{i}"},
        )
        if resp.status_code == 202:
            accepted += 1
        elif resp.status_code == 429:
            refused += 1
            assert "上限" in resp.json()["error"], resp.text
        else:  # pragma: no cover —— 出现即是 bug
            pytest.fail(f"过载时返回了 {resp.status_code}：{resp.text}")

        pending = await ctx.task_store.list_tasks(status=TaskStatus.PENDING, limit=1000)
        peak_backlog = max(peak_backlog, len(pending))

    assert refused > 0, "投了 10 倍的量却一次都没拒 —— 背压根本没生效"
    assert accepted > 0, "全被拒了，说明闸门卡死而不是限流"
    assert peak_backlog <= MAX_BACKLOG, (
        f"积压峰值 {peak_backlog} 超过上限 {MAX_BACKLOG} —— 队列不是有界的"
    )

    # 进程还活着，且服务仍能正常应答
    assert (await client.get("/admin/health")).status_code == 200


async def test_refusal_does_not_create_orphan_task_records(
    client: httpx.AsyncClient,
) -> None:
    """拒收发生在**建记录之前**。建完再拒等于白建一条，还会污染积压统计。"""
    await client.post("/kb", json={"kb_id": KB})
    ctx = client._transport.app.state.ctx  # noqa: SLF001

    for i in range(MAX_BACKLOG * 4):
        await client.post(
            "/ingest",
            json={"kb_id": KB, "source": "slow.stub", "idempotency_key": f"x-{i}"},
        )

    all_tasks = await ctx.task_store.list_tasks(limit=1000)
    # 被拒的那些不该留下任何痕迹：总记录数不会逼近投递次数
    assert len(all_tasks) < MAX_BACKLOG * 4, (
        f"投了 {MAX_BACKLOG * 4} 次，却留下了 {len(all_tasks)} 条记录 —— "
        "拒收发生在建记录之后"
    )
