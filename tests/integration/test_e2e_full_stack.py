"""端到端链路跑在**全部真实后端**上：PostgreSQL + Milvus（Checkpoint D）。

与 `tests/e2e/test_ingest_search.py`（全内存）相比，**只改了配置里的
backends 段**。断言逻辑复用同一批替身与轮询函数，一行不动。

若本文件需要为跑通而修改任何断言，那说明抽象漏了 —— 该改的是抽象，
不是这里。这正是 plan"先内存后真实"要证明的事。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from comet_rag.api.main import create_app
from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    ServerConfig,
    SqlDatabaseConfig,
    VectorDatabaseConfig,
)
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from tests.e2e.test_ingest_search import (  # 复用替身，不重写
    DIM,
    STUB_TYPE,
    KeywordEmbedding,
    _use_stub_loader,
    poll_until_terminal,
)
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration

#: 与其他集成用例区分，避免残留 collection 互相干扰
KB = "kb-fullstack"
PREFIX = "fullstack"


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
    def _chunk(text_: str, config: PipelineConfig) -> list[str]:
        return [line for line in text_.splitlines() if line.strip()]

    yield


def make_config(milvus_uri: str) -> APPConfig:
    """**与全内存版唯一的差别就是 backends 段与两处连接配置。**"""
    return APPConfig(
        server_config=ServerConfig(app_name="comet-rag-full", host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub-embed", dim=DIM
            ),
            database=SqlDatabaseConfig(
                host="localhost",
                port=5432,
                username="comet",
                password="comet",  # noqa: S106 —— 本地 compose 的固定测试凭据
                database="comet_rag",
            ),
            vector_database=VectorDatabaseConfig(
                endpoint=milvus_uri, collection_name="unused"
            ),
        ),
        backends=BackendsConfig(
            task_store=Backend.POSTGRES,
            vector_store=Backend.MILVUS,
        ),
    )


async def _clean_postgres(dsn: str) -> None:
    """清表。锁等待有上限，拿不到就报错而不是静默挂起 ——
    实现与理由见 `conftest.truncate_tables`。"""
    await truncate_tables(dsn, "tasks", "knowledge_bases")


@pytest.fixture
async def client(
    document: Path, postgres_dsn: str, milvus_uri: str
) -> AsyncIterator[httpx.AsyncClient]:
    await _clean_postgres(postgres_dsn)
    config = make_config(milvus_uri)
    app = create_app(
        config,
        embedding_model=KeywordEmbedding(),
        pipeline_config=PipelineConfig(embed_batch_size=2, max_concurrency=4),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        _use_stub_loader(app.state.ctx, document)
        with contextlib.suppress(Exception):
            await app.state.ctx.vector_store.adrop_collection(KB)
        yield http
        with contextlib.suppress(Exception):
            await app.state.ctx.vector_store.adrop_collection(KB)
    await _clean_postgres(postgres_dsn)


# ── 与全内存版**逐字相同**的断言 ───────────────────────────────────────────


async def test_ingest_then_search_on_real_backends(
    client: httpx.AsyncClient,
) -> None:
    """Checkpoint D 的验收用例。

    这条链路里：任务状态在 PostgreSQL、向量在 Milvus、知识库元数据在
    PostgreSQL —— 全是真实中间件。断言与全内存版一字不差。
    """
    created = await client.post("/kb", json={"kb_id": KB})
    assert created.status_code == 201, created.text
    assert created.json()["embedding_dim"] == DIM

    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    assert submitted.status_code == 202
    done = await poll_until_terminal(client, submitted.json()["task_id"], timeout=20.0)
    assert done["status"] == "succeeded", done
    assert done["result"]["chunk_count"] == 3

    found = await client.post(
        "/search", json={"kb_id": KB, "query": "香蕉怎么种", "top_k": 1}
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["chunks"], "检索不到刚入库的内容"
    assert "香蕉" in body["chunks"][0]["text"]
    assert body["chunks"][0]["metadata"]["kb_id"] == KB


async def test_reingest_replaces_instead_of_duplicating(
    client: httpx.AsyncClient,
) -> None:
    """幂等重入库在 Milvus 上同样成立 —— chunk id 是稳定的 SHA256。"""
    await client.post("/kb", json={"kb_id": KB})
    for _ in range(2):
        submitted = await client.post(
            "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
        )
        await poll_until_terminal(client, submitted.json()["task_id"], timeout=20.0)

    info = (await client.get(f"/kb/{KB}")).json()
    assert info["chunk_count"] == 3, "重复入库产生了副本"


async def test_metadata_filter_works_on_milvus(client: httpx.AsyncClient) -> None:
    """结构化 dict 过滤要真的翻译成 Milvus 表达式并生效。"""
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest",
        json={"kb_id": KB, "source": "fruits.stub", "metadata": {"batch": "v1"}},
    )
    await poll_until_terminal(client, submitted.json()["task_id"], timeout=20.0)

    hit = await client.post(
        "/search",
        json={"kb_id": KB, "query": "苹果", "top_k": 10, "filter": {"batch": "v1"}},
    )
    miss = await client.post(
        "/search",
        json={"kb_id": KB, "query": "苹果", "top_k": 10, "filter": {"batch": "v2"}},
    )

    assert len(hit.json()["chunks"]) == 3
    assert miss.json()["chunks"] == []


async def test_deleting_kb_removes_milvus_collection(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    await poll_until_terminal(client, submitted.json()["task_id"], timeout=20.0)

    assert (await client.delete(f"/kb/{KB}")).status_code == 204

    gone = await client.post("/search", json={"kb_id": KB, "query": "苹果"})
    assert gone.status_code == 404
