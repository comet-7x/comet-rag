"""端到端：建库 → 入库 → 轮询 → 检索。**不需要 docker**。

这是 plan 里 Checkpoint C 的验收用例，也是"先内存后真实"这条策略的兑现：
整套抽象（TaskStore / TaskExecutor / BaseVectorStore）在零中间件下就能验证
是否成立。Phase 4 换上 Postgres / Milvus / ARQ 后，本用例应当**一字不改**
地继续通过 —— 只换注入的实现。

走的是 `create_app()` 这条**真实装配路径**，不是测试里另抄一份接线。
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
    ServerConfig,
)
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from comet_rag.services.ingestion import IngestRunner, register_ingest_runner

pytestmark = pytest.mark.e2e

DIM = 3
KB = "kb-e2e"
STUB_TYPE = "stub"

DOCUMENT = {
    "苹果": "苹果富含维生素与膳食纤维。",
    "香蕉": "香蕉适合在热带地区种植。",
    "橙子": "橙子的酸度取决于成熟度。",
}


# ── 替身：唯一被替换的是"打网络的那两个" ───────────────────────────────────


class KeywordEmbedding(BaseEmbeddingModel):
    """按关键词映射到三维空间，让"该命中哪条"完全可预测。"""

    def _vector(self, text: str) -> list[float]:
        return [
            1.0 if "苹果" in text else 0.0,
            1.0 if "香蕉" in text else 0.0,
            1.0 if "橙子" in text else 0.0,
        ]

    def embed(self, data, **kwargs):  # pragma: no cover
        return self._vector(str(data))

    async def aembed(self, data, **kwargs):
        return self._vector(str(data))

    async def close_client(self) -> None:
        return None


class LocalStubLoader(BaseLoader):
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, source: SourceContent | str, *args, **kwargs) -> LoaderContent:
        if not isinstance(source, SourceContent):
            source = SourceContent(str(source))
        return LoaderContent(
            path=self._path,
            source=source,
            is_temp=False,
            metadata={"file_type": STUB_TYPE, "file_name": self._path.name},
        )

    async def aload(self, source, *args, **kwargs) -> LoaderContent:
        return self.load(source)

    def cleanup(self) -> None:
        return None


# ── 夹具 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "fruits.stub"
    path.write_text("\n".join(DOCUMENT.values()), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def hooks(document: Path):
    @PipelineHooks.extractor(STUB_TYPE)
    def _extract(lc: LoaderContent, config: PipelineConfig) -> str:
        return lc.path.read_text(encoding="utf-8")

    @PipelineHooks.chunker(STUB_TYPE)
    def _chunk(text: str, config: PipelineConfig) -> list[str]:
        return [line for line in text.splitlines() if line.strip()]

    yield


def make_config() -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(app_name="comet-rag-e2e", host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub-embed", dim=DIM
            )
        ),
    )


@pytest.fixture
async def client(document: Path) -> AsyncIterator[httpx.AsyncClient]:
    """真实 ASGI 应用，只把 embedding 模型与 loader 换成替身。

    向量库、任务库、执行器全部走配置里的 memory 后端 —— 不是测试特设的分支。
    """
    app = create_app(
        make_config(),
        embedding_model=KeywordEmbedding(),
        vector_store=InMemoryVectorStore(),
        pipeline_config=PipelineConfig(embed_batch_size=2, max_concurrency=4),
    )
    transport = httpx.ASGITransport(app=app)

    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        # ASGITransport 默认不跑 lifespan，手动进入
        app.router.lifespan_context(app),
    ):
        _use_stub_loader(app.state.ctx, document)
        yield http


def _use_stub_loader(ctx: Any, document: Path) -> None:
    """只替换 loader 这一个依赖。

    真实 `AutoLoader` 会把 "fruits.stub" 判为未知来源；而其余部件
    （向量库、任务库、执行器、路由、异常映射）全是 `create_app` 装配的真货 ——
    替身越少，端到端测试越有意义。
    """
    register_ingest_runner(
        IngestRunner(
            embedding_model=ctx.embedding_model,
            vector_store=ctx.vector_store,
            embedding_dim=ctx.embedding_dim,
            loader=LocalStubLoader(document),
            config=PipelineConfig(embed_batch_size=2, max_concurrency=4),
        )
    )


async def poll_until_terminal(
    http: httpx.AsyncClient, task_id: str, *, timeout: float = 5.0
) -> dict[str, Any]:
    """轮询到终态。**只经公开 API 观察**，不碰内部对象 ——
    这样 Phase 4 换后端时本用例不需要任何修改。"""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen_stages: list[str] = []
    while loop.time() < deadline:
        resp = await http.get(f"/tasks/{task_id}")
        resp.raise_for_status()
        body = resp.json()
        stage = body.get("stage")
        if stage and (not seen_stages or seen_stages[-1] != stage):
            seen_stages.append(stage)
        if body["status"] in ("succeeded", "failed", "cancelled"):
            body["_seen_stages"] = seen_stages
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未结束")


# ── Checkpoint C 主用例 ────────────────────────────────────────────────────


async def test_ingest_then_search(client: httpx.AsyncClient) -> None:
    """建库 → 入库 → 轮询见阶段推进 → 检索命中刚入库的内容。"""
    created = await client.post("/kb", json={"kb_id": KB})
    assert created.status_code == 201, created.text
    assert created.json()["embedding_dim"] == DIM

    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    assert submitted.status_code == 202, submitted.text
    task_id = submitted.json()["task_id"]

    done = await poll_until_terminal(client, task_id)
    assert done["status"] == "succeeded", done
    assert done["result"]["chunk_count"] == 3
    assert done["_seen_stages"], "轮询期间应能看到 stage 字段"

    found = await client.post(
        "/search", json={"kb_id": KB, "query": "香蕉怎么种", "top_k": 1}
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["chunks"], "检索不到刚入库的内容"
    assert "香蕉" in body["chunks"][0]["text"]
    assert body["chunks"][0]["metadata"]["kb_id"] == KB


async def test_chunk_metadata_carries_kb_id(client: httpx.AsyncClient) -> None:
    """租户维度必须落到每一条向量上（spec A5）。"""
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    await poll_until_terminal(client, submitted.json()["task_id"])

    found = await client.post(
        "/search", json={"kb_id": KB, "query": "苹果", "top_k": 10}
    )

    for chunk in found.json()["chunks"]:
        assert chunk["metadata"]["kb_id"] == KB
        assert "source_id" in chunk["metadata"]


async def test_idempotency_key_does_not_duplicate_task(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/kb", json={"kb_id": KB})
    payload = {"kb_id": KB, "source": "fruits.stub", "idempotency_key": "doc-1"}

    first = await client.post("/ingest", json=payload)
    again = await client.post("/ingest", json=payload)

    assert first.json()["task_id"] == again.json()["task_id"]


async def test_search_does_not_leak_across_knowledge_bases(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/kb", json={"kb_id": KB})
    await client.post("/kb", json={"kb_id": "kb-other"})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    await poll_until_terminal(client, submitted.json()["task_id"])

    found = await client.post(
        "/search", json={"kb_id": "kb-other", "query": "苹果", "top_k": 10}
    )

    assert found.json()["chunks"] == []


# ── API 契约 ───────────────────────────────────────────────────────────────


async def test_task_view_hides_internal_fields(client: httpx.AsyncClient) -> None:
    """不得外泄 traceback、worker_id、乐观锁版本、context。"""
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    body = await poll_until_terminal(client, submitted.json()["task_id"])

    for leaked in ("worker_id", "version", "idempotency_key", "context"):
        assert leaked not in body


async def test_unknown_task_returns_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/tasks/根本不存在")

    assert resp.status_code == 404
    assert "error" in resp.json()


async def test_unknown_knowledge_base_returns_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post("/search", json={"kb_id": "从未建过", "query": "苹果"})

    assert resp.status_code == 404


async def test_invalid_payload_returns_422(client: httpx.AsyncClient) -> None:
    resp = await client.post("/ingest", json={"kb_id": "", "source": ""})

    assert resp.status_code == 422


async def test_retry_of_non_failed_task_returns_400(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    task_id = submitted.json()["task_id"]
    await poll_until_terminal(client, task_id)

    resp = await client.post(f"/tasks/{task_id}/retry")

    assert resp.status_code == 400
    assert "FAILED" in resp.json()["error"]


async def test_task_list_is_filterable(client: httpx.AsyncClient) -> None:
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    await poll_until_terminal(client, submitted.json()["task_id"])

    resp = await client.get("/tasks", params={"kind": "ingest"})

    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


async def test_knowledge_base_lifecycle(client: httpx.AsyncClient) -> None:
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    await poll_until_terminal(client, submitted.json()["task_id"])

    info = await client.get(f"/kb/{KB}")
    assert info.json()["chunk_count"] == 3

    deleted = await client.delete(f"/kb/{KB}")
    assert deleted.status_code == 204

    gone = await client.post("/search", json={"kb_id": KB, "query": "苹果"})
    assert gone.status_code == 404


async def test_health_endpoint(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/health")).status_code == 200
