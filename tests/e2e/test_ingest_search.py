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
    IngestPolicyConfig,
    ServerConfig,
)
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.providers.embedding.base import BaseEmbeddingModel
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

    async def _aembed(self, data, **kwargs):
        return self._vector(str(data))

    async def close_client(self) -> None:
        return None


class LocalStubLoader(BaseLoader):
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self, source: SourceContent | str, *args, **kwargs) -> LoaderContent:
        if not isinstance(source, SourceContent):
            source = SourceContent(str(source))
        return LoaderContent(
            path=self._path,
            source=source,
            is_temp=False,
            metadata={"file_type": STUB_TYPE, "file_name": self._path.name},
        )

    async def _aload(self, source, *args, **kwargs) -> LoaderContent:
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
        # 这些用例走的正是"服务端读本地文件"这条**危险能力**，
        # 所以必须显式打开 —— 默认是拒绝的（PR 评审 #4）。
        ingest_policy=IngestPolicyConfig(allow_local=True),
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


def ctx_of(client: httpx.AsyncClient) -> Any:
    """从 client 摸回应用的 `Context`（只用来读积压、降级这些内部计数）。

    httpx 的 `_transport` 是私有属性，而 `ASGITransport.app` 静态上只是
    "一个 ASGI 可调用对象"，看不见 `.state`。这层穿透收在一个函数里，
    好过在每个用例里各撒一份 `# noqa` 加类型忽略。
    """
    transport: Any = client._transport  # noqa: SLF001
    return transport.app.state.ctx


def _use_stub_loader(
    ctx: Any, document: Path, config: PipelineConfig | None = None
) -> None:
    """只替换 loader 这一个依赖。

    真实 `AutoLoader` 会把 "fruits.stub" 判为未知来源；而其余部件
    （向量库、任务库、执行器、路由、异常映射）全是 `create_app` 装配的真货 ——
    替身越少，端到端测试越有意义。

    ⚠️ `config` 必须显式传：本函数会**重新注册** runner，从而覆盖掉
    `create_app(pipeline_config=...)` 装配的那一份。默认值刻意保持小批量
    （2/4），因为端到端用例要的是"看得见阶段推进"，不是吞吐。
    基准测试必须传自己的配置 —— 否则量到的是这里的默认值，而不是被测的东西。
    这个坑真的踩过：`test_embedding_overlaps_io` 一开始怎么算都对不上，
    根因就是它以为自己配的是 32/16，实际跑的是 2/4。
    """
    register_ingest_runner(
        IngestRunner(
            embedding_model=ctx.embedding_model,
            vector_store=ctx.vector_store,
            knowledge_base=ctx.knowledge_base,
            loader=LocalStubLoader(document),
            config=config or PipelineConfig(embed_batch_size=2, max_concurrency=4),
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
    assert created.json()["embedding_model"] == "stub-embed"

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
    assert info.json()["embedding_model"] == "stub-embed"

    deleted = await client.delete(f"/kb/{KB}")
    assert deleted.status_code == 204

    gone = await client.post("/search", json={"kb_id": KB, "query": "苹果"})
    assert gone.status_code == 404


async def test_health_endpoint(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/health")).status_code == 200


# ── 知识库元数据（T19）──────────────────────────────────────────────────────


async def test_ingest_into_unknown_kb_returns_failed_task(
    client: httpx.AsyncClient,
) -> None:
    """往不存在的知识库灌数据必须失败，不能顺手建一个 ——
    打错一个字就凭空多出一个库，而且没人会发现。"""
    submitted = await client.post(
        "/ingest", json={"kb_id": "从未建过", "source": "fruits.stub"}
    )
    done = await poll_until_terminal(client, submitted.json()["task_id"])

    assert done["status"] == "failed"


async def test_kb_records_embedding_model(client: httpx.AsyncClient) -> None:
    """spec A12：这个字段是知识库表存在的全部理由。"""
    await client.post("/kb", json={"kb_id": KB, "description": "水果知识库"})

    info = (await client.get(f"/kb/{KB}")).json()

    assert info["embedding_model"] == "stub-embed"
    assert info["embedding_dim"] == DIM
    assert info["description"] == "水果知识库"


async def test_kb_creation_is_idempotent(client: httpx.AsyncClient) -> None:
    first = await client.post("/kb", json={"kb_id": KB, "name": "原名"})
    again = await client.post("/kb", json={"kb_id": KB, "name": "新名"})

    assert again.status_code == 201
    assert again.json()["created_at"] == first.json()["created_at"]
    assert again.json()["name"] == "原名"


async def test_kb_list(client: httpx.AsyncClient) -> None:
    await client.post("/kb", json={"kb_id": "kb-a"})
    await client.post("/kb", json={"kb_id": "kb-b"})

    body = (await client.get("/kb")).json()

    assert body["total"] >= 2
    assert {k["kb_id"] for k in body["knowledge_bases"]} >= {"kb-a", "kb-b"}


async def test_changing_embedding_model_returns_409(
    client: httpx.AsyncClient,
) -> None:
    """**spec A12 在 HTTP 层的兑现**。

    同维度的两个不同模型产出的向量落在完全不同的语义空间里，混用不报错、
    只是检索静默劣化，事后还分不清哪些 chunk 该重算 —— 必须当场拒绝。

    这里直接改服务持有的模型名来模拟"改了配置重启"，因为那是唯一能在
    单个应用实例内复现该场景的方式。
    """
    await client.post("/kb", json={"kb_id": KB})

    # 模拟运维改了 config.yaml 里的 embedding 模型后重启
    ctx = ctx_of(client)
    ctx.knowledge_base._model = "换成了别的模型"

    conflicted = await client.post("/kb", json={"kb_id": KB})

    assert conflicted.status_code == 409
    body = conflicted.json()
    # 报错要说清后果，而不只是干巴巴一句"不匹配"
    assert "语义空间" in body["error"]
    assert "trace_id" in body


# ── 来源准入（PR 评审 #4）──────────────────────────────────────────────────


async def test_ingest_refuses_arbitrary_server_paths_by_default(
    document: Path,
) -> None:
    """**默认部署下 `/ingest` 不许读服务器上的任意文件。**

    本文件其余用例都显式打开了 `allow_local`（它们验的正是这条危险能力），
    所以这里另起一个**默认配置**的应用 —— 否则测的是被放开之后的样子。
    """
    app = create_app(
        APPConfig(
            server_config=ServerConfig(app_name="locked", host="127.0.0.1", port=0),
            infrastructure_config=InfrastructureConfig(
                embedding_model=EmbeddingModelConfig(
                    base_url="http://unused", model_name="stub-embed", dim=DIM
                )
            ),
        ),
        embedding_model=KeywordEmbedding(),
        vector_store=InMemoryVectorStore(),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        await http.post("/kb", json={"kb_id": KB})

        for source in ("/etc/passwd", "../../etc/shadow", str(document)):
            resp = await http.post("/ingest", json={"kb_id": KB, "source": source})
            assert resp.status_code == 403, (
                f"{source} 竟然被受理了（{resp.status_code}）—— 任意文件读取通道"
            )

        # 拒绝必须发生在**建任务之前**：不该留下任何任务记录
        listed = await http.get("/tasks")
        assert listed.json()["tasks"] == [], "被拒的请求留下了任务记录"


async def test_ingest_refuses_ssrf_targets(client: httpx.AsyncClient) -> None:
    """云元数据服务是 SSRF 最经典的目标 —— 那里有临时凭据。

    注意本用例用的是已经 `allow_local=True` 的那个 client：
    放开本地路径**不等于**放开内网访问，两者是各自独立的开关。
    """
    await client.post("/kb", json={"kb_id": KB})

    for source in (
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:6379/",
        "http://localhost:5432/",
        "file:///etc/passwd",
    ):
        resp = await client.post("/ingest", json={"kb_id": KB, "source": source})
        assert resp.status_code == 403, f"{source} 没被挡住（{resp.status_code}）"
