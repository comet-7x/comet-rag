"""端到端链路跑在 **PostgreSQL 后端**上（Checkpoint D）。

复用 `tests/e2e/test_ingest_search.py` 的全部替身与断言逻辑，**只换配置里的
后端选项** —— 这正是 plan"先内存后真实"要证明的事：换后端只改配置，
业务代码与测试断言一行不动。

若本文件需要为了跑通而修改任何断言，那说明抽象漏了 —— 该改的是抽象。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from comet_rag.api.main import create_app
from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    ServerConfig,
    SqlDatabaseConfig,
)
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.database import Database
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from tests.e2e.test_ingest_search import (  # 复用替身，不重写
    DIM,
    KB,
    STUB_TYPE,
    KeywordEmbedding,
    _use_stub_loader,
    poll_until_terminal,
)

pytestmark = pytest.mark.integration


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


def make_postgres_config() -> APPConfig:
    """**与内存版唯一的差别就是这里的 backends 段。**"""
    return APPConfig(
        server_config=ServerConfig(app_name="comet-rag-pg", host="127.0.0.1", port=0),
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
        ),
        backends=BackendsConfig(
            task_store=Backend.POSTGRES,  # ← 换的就是这一行
            vector_store=Backend.MEMORY,  # Milvus 留给 T21
        ),
    )


async def _clean(dsn: str) -> None:
    db = Database(dsn)
    try:
        async with db.session() as session:
            for table in ("tasks", "knowledge_bases"):
                exists = (
                    await session.execute(text(f"SELECT to_regclass('{table}')"))
                ).scalar_one()
                if exists is None:
                    pytest.skip(f"{table} 表不存在，先跑 `uv run alembic upgrade head`")
        async with db.transaction() as session:
            await session.execute(text("TRUNCATE TABLE tasks CASCADE"))
            await session.execute(text("TRUNCATE TABLE knowledge_bases CASCADE"))
    finally:
        await db.aclose()


@pytest.fixture
async def client(document: Path, postgres_dsn: str) -> AsyncIterator[httpx.AsyncClient]:
    await _clean(postgres_dsn)
    app = create_app(
        make_postgres_config(),
        embedding_model=KeywordEmbedding(),
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
    await _clean(postgres_dsn)


# ── 与内存版**逐字相同**的断言 ─────────────────────────────────────────────


async def test_ingest_then_search_on_postgres(client: httpx.AsyncClient) -> None:
    created = await client.post("/kb", json={"kb_id": KB})
    assert created.status_code == 201, created.text

    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    assert submitted.status_code == 202
    task_id = submitted.json()["task_id"]

    done = await poll_until_terminal(client, task_id)
    assert done["status"] == "succeeded", done
    assert done["result"]["chunk_count"] == 3

    found = await client.post(
        "/search", json={"kb_id": KB, "query": "香蕉怎么种", "top_k": 1}
    )
    assert found.status_code == 200
    assert "香蕉" in found.json()["chunks"][0]["text"]


async def test_task_survives_a_fresh_read(client: httpx.AsyncClient) -> None:
    """内存版做不到的验证：任务真的落库了。

    每次 `GET /tasks/{id}` 都是一次独立的数据库读 —— 若序列化有损，
    stage_history、result、context 这些结构化字段会在这里露馅。
    """
    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    done = await poll_until_terminal(client, submitted.json()["task_id"])

    assert done["status"] == "succeeded"
    assert [s["stage"] for s in done["stage_history"]] == [
        "extracting",
        "chunking",
        "indexing",
    ]
    assert done["result"]["chunk_count"] == 3


async def test_idempotency_key_is_enforced_by_the_database(
    client: httpx.AsyncClient,
) -> None:
    """Postgres 版靠唯一约束保证，不是靠应用层"先查后插"。"""
    await client.post("/kb", json={"kb_id": KB})
    payload = {"kb_id": KB, "source": "fruits.stub", "idempotency_key": "doc-1"}

    first = await client.post("/ingest", json=payload)
    again = await client.post("/ingest", json=payload)

    assert first.json()["task_id"] == again.json()["task_id"]


async def test_knowledge_base_metadata_persists(client: httpx.AsyncClient) -> None:
    await client.post(
        "/kb", json={"kb_id": KB, "name": "水果库", "description": "示例"}
    )

    info = (await client.get(f"/kb/{KB}")).json()

    assert info["name"] == "水果库"
    assert info["description"] == "示例"
    assert info["embedding_model"] == "stub-embed"
