"""完整部署形态：**API 进程 + preprocessor + embedder**，全真后端（T23）。

这是 plan Checkpoint E 的验收用例，也是整个项目第一次把三个角色同时摆上：

    POST /ingest ──► Redis(comet:cpu) ──► preprocessor  extracting/chunking
                                                │ 移交
                                                ▼
                     Redis(comet:io)  ──► embedder      indexing
                                                │
    GET /tasks/{id} ◄── PostgreSQL ◄────────────┘        Milvus ◄── 向量

**三个角色各自 `build_context()`**，各有各的连接池 —— 除了 Redis、PostgreSQL、
Milvus 之外没有任何共享对象。任何"其实是同一个进程所以看得见"的假通过，
都会在这个布置下露馅。

替身只有两个：embedding 模型与 loader（它们要打网络）。worker 侧的替身
经 `build_settings(..., embedding_model=...)` 注入，走的是 `build_context`
这条**真实装配路径**，不是测试里另抄一份接线。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from arq.worker import Worker, create_worker
from sqlalchemy import text

from comet_rag.api.main import create_app
from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    IngestPolicyConfig,
    RedisConfig,
    ServerConfig,
    SqlDatabaseConfig,
    VectorDatabaseConfig,
)
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.database import Database
from comet_rag.tasks.executor_arq import LANE_QUEUES
from comet_rag.workers import build_settings
from comet_rag.workers.embedder import PROFILE as EMBEDDER
from comet_rag.workers.preprocessor import PROFILE as PREPROCESSOR
from tests.e2e.test_ingest_search import (  # 复用替身，不重写
    DIM,
    STUB_TYPE,
    KeywordEmbedding,
    LocalStubLoader,
    poll_until_terminal,
)
from tests.integration.conftest import truncate_tables

pytestmark = pytest.mark.integration

KB = "kb-workers"
PIPELINE = PipelineConfig(embed_batch_size=2, max_concurrency=4)

#: 专供本用例的 redis db，与开发机上真跑着的 worker 隔开
TEST_REDIS_DB = 15


@pytest.fixture
def document(tmp_path: Path) -> Path:
    path = tmp_path / "fruits.stub"
    path.write_text(
        "苹果富含维生素与膳食纤维。\n香蕉适合在热带地区种植。\n橙子的酸度取决于成熟度。",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def hooks() -> Any:
    @PipelineHooks.extractor(STUB_TYPE)
    def _extract(lc: Any, config: PipelineConfig) -> str:
        return lc.path.read_text(encoding="utf-8")

    @PipelineHooks.chunker(STUB_TYPE)
    def _chunk(text_: str, config: PipelineConfig) -> list[str]:
        return [line for line in text_.splitlines() if line.strip()]

    yield


def make_config(milvus_uri: str) -> APPConfig:
    """**全真后端 + arq 执行器**。与全内存版的差别就是 backends 这一段。"""
    return APPConfig(
        server_config=ServerConfig(
            app_name="comet-rag-workers", host="127.0.0.1", port=0
        ),
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
            # 队列名必须是生产那两个（本用例用的就是生产 PROFILE），所以改用
            # **独立的 redis db** 来隔离：否则 `_drain_queues` 会把开发机上
            # 正在跑的 comet:cpu / comet:io 一起删掉。
            redis=RedisConfig(host="localhost", port=6379, db_index=TEST_REDIS_DB),
        ),
        backends=BackendsConfig(
            task_store=Backend.POSTGRES,
            vector_store=Backend.MILVUS,
            task_executor=Backend.ARQ,  # ← 本用例的主角
        ),
        # 这些用例走的正是"服务端读本地文件"这条**危险能力**，
        # 所以必须显式打开 —— 默认是拒绝的（PR 评审 #4）。
        ingest_policy=IngestPolicyConfig(allow_local=True),
    )


async def _clean(dsn: str) -> None:
    """清表。锁等待有上限，拿不到就报错而不是静默挂起 ——
    实现与理由见 `conftest.truncate_tables`。"""
    await truncate_tables(dsn, "tasks", "knowledge_bases")


async def _worker_id_of(dsn: str, task_id: str) -> str | None:
    db = Database(dsn)
    try:
        async with db.session() as session:
            return (
                await session.execute(
                    text("SELECT worker_id FROM tasks WHERE task_id = :tid"),
                    {"tid": task_id},
                )
            ).scalar_one()
    finally:
        await db.aclose()


async def _drain_queues(pool: ArqRedis) -> None:
    """清掉上一轮遗留的 job，否则失败会以"莫名其妙的额外任务"形式串到下个用例。"""
    for queue in LANE_QUEUES.values():
        await pool.delete(queue)


def _make_worker(profile: Any, config: APPConfig) -> Worker:
    """按**生产用的 profile** 起 worker，只把并发与轮询压小以便测试。

    `PROFILE` 用的就是 `workers/preprocessor.py` 里那份 —— lane、队列名、
    扩容画像都不是测试另编的，改错了生产参数这里会跟着红。
    """
    settings = build_settings(
        profile,
        config=config,
        embedding_model=KeywordEmbedding(),
        pipeline_config=PIPELINE,
    )
    return create_worker(
        settings,
        poll_delay=0.02,
        handle_signals=False,
        keep_result=2,
        log_results=False,
    )


async def _await_startup(*workers: Worker, timeout: float = 20.0) -> None:
    """等 arq 把 `on_startup` 跑完（Context 装配好）。

    `on_startup` 在 `worker.main()` 里执行，创建任务后并不会立刻完成 ——
    不等就去读 `ctx["context"]` 会拿到 KeyError，而且是偶发的。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if all("context" in w.ctx for w in workers):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker 未在超时内完成启动装配")


@contextlib.asynccontextmanager
async def _running(*workers: Worker) -> AsyncIterator[None]:
    runners = [asyncio.create_task(w.async_run()) for w in workers]
    try:
        yield
    finally:
        for r in runners:
            r.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        for w in workers:
            for job in list(w.tasks.values()):
                job.cancel()
            if w.tasks:
                await asyncio.gather(*w.tasks.values(), return_exceptions=True)
            await _close_worker_context(w)


async def _close_worker_context(worker: Worker) -> None:
    context = worker.ctx.get("context")
    if context is not None:
        await context.aclose()
        worker.ctx.pop("context", None)


@pytest.fixture
async def deployment(
    document: Path,
    postgres_dsn: str,
    redis_url: str,  # noqa: ARG001 —— 只为触发"Redis 没起就跳过"的探测
    milvus_uri: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, list[Worker]]]:
    await _clean(postgres_dsn)
    config = make_config(milvus_uri)
    # 用 config 里那份连接（db=15），不是 fixture 的 db=0 —— 否则清的是别人的队列
    pool = await create_pool(
        RedisSettings.from_dsn(config.infrastructure_config.redis.url)
    )
    await _drain_queues(pool)

    app = create_app(
        config,
        embedding_model=KeywordEmbedding(),
        pipeline_config=PIPELINE,
    )
    workers = [_make_worker(PREPROCESSOR, config), _make_worker(EMBEDDER, config)]

    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        app.router.lifespan_context(app),
    ):
        with contextlib.suppress(Exception):
            await app.state.ctx.vector_store.adrop_collection(KB)
        async with _running(*workers):
            await _await_startup(*workers)
            # runner 注册表是**进程级全局**的，而这里三个 Context 挤在同一个
            # 进程里，最后一次 wire_runners 说了算。真实部署下各进程各有一份
            # 注册表，不存在这个问题；测试里统一绑上带替身 loader 的那份。
            _wire_stub_runner(workers[-1], document)
            yield http, workers
        with contextlib.suppress(Exception):
            await app.state.ctx.vector_store.adrop_collection(KB)

    await pool.aclose()
    await _clean(postgres_dsn)


def _wire_stub_runner(worker: Worker, document: Path) -> None:
    """把 loader 换成替身。其余依赖全取自 worker 自己装配出来的 Context。"""
    from comet_rag.services.ingestion import (  # noqa: PLC0415
        IngestRunner,
        register_ingest_runner,
    )

    context = worker.ctx["context"]
    register_ingest_runner(
        IngestRunner(
            embedding_model=context.embedding_model,
            vector_store=context.vector_store,
            knowledge_base=context.knowledge_base,
            loader=LocalStubLoader(document),
            config=PIPELINE,
        )
    )


# ── Checkpoint E 主用例 ────────────────────────────────────────────────────


async def test_api_submits_workers_consume_task_succeeds(
    deployment: tuple[httpx.AsyncClient, list[Worker]],
) -> None:
    """T23 的验收：API 提交 → worker 消费 → 状态推进至 SUCCEEDED。

    API 进程里**没有执行器在跑任务** —— 它的 `ArqExecutor` 只入队。
    任务能走完，只可能是两个 worker 真的消费了。
    """
    client, workers = deployment

    created = await client.post("/kb", json={"kb_id": KB})
    assert created.status_code == 201, created.text

    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    assert submitted.status_code == 202

    done = await poll_until_terminal(client, submitted.json()["task_id"], timeout=30.0)
    assert done["status"] == "succeeded", done
    assert done["result"]["chunk_count"] == 3
    assert [s["stage"] for s in done["stage_history"]] == [
        "extracting",
        "chunking",
        "indexing",
    ]

    found = await client.post(
        "/search", json={"kb_id": KB, "query": "香蕉怎么种", "top_k": 1}
    )
    assert found.status_code == 200, found.text
    assert "香蕉" in found.json()["chunks"][0]["text"]


async def test_stages_land_on_the_lane_that_declared_them(
    deployment: tuple[httpx.AsyncClient, list[Worker]], postgres_dsn: str
) -> None:
    """分道必须真的把活分开，而不是两个 worker 干着同样的事。

    `worker_id` 是内部字段、刻意不出现在公开 API 里（见 `schemas/task.py`），
    所以这里破例直连数据库读它。为一条断言开这个口子是值得的：没有它，
    "两个 worker 都在跑全流程" 这种失效从外部完全看不出来。
    """
    client, workers = deployment
    preprocessor, embedder = workers

    await client.post("/kb", json={"kb_id": KB})
    submitted = await client.post(
        "/ingest", json={"kb_id": KB, "source": "fruits.stub"}
    )
    task_id = submitted.json()["task_id"]
    done = await poll_until_terminal(client, task_id, timeout=30.0)
    assert done["status"] == "succeeded", done

    cpu_id = preprocessor.ctx["executor"].worker_id
    io_id = embedder.ctx["executor"].worker_id
    assert cpu_id != io_id

    last_worker = await _worker_id_of(postgres_dsn, task_id)
    assert last_worker == io_id, (
        f"最后一个阶段跑在 {last_worker}，期望 embedder({io_id}) —— "
        "分道没生效，或者 indexing 被 preprocessor 抢走了"
    )


async def test_workers_reuse_one_http_client_across_tasks(
    deployment: tuple[httpx.AsyncClient, list[Worker]],
) -> None:
    """spec S4-4 / A3：**跨任务复用连接池**，不是每个任务现建现销。

    这是 T22 挂账、由 T23 的共享 `Context` 兑现的那条验收标准。
    每个任务各自 new 一个模型客户端的话，连接数会随任务数线性涨，
    高频入库时光 TCP + TLS 握手就能吃掉大部分耗时。
    """
    client, workers = deployment
    embedder = workers[-1]
    model_before = embedder.ctx["context"].embedding_model

    await client.post("/kb", json={"kb_id": KB})
    for i in range(3):
        submitted = await client.post(
            "/ingest",
            json={
                "kb_id": KB,
                "source": "fruits.stub",
                "idempotency_key": f"doc-{i}",
            },
        )
        done = await poll_until_terminal(
            client, submitted.json()["task_id"], timeout=30.0
        )
        assert done["status"] == "succeeded", done

    assert embedder.ctx["context"].embedding_model is model_before, (
        "worker 在任务之间换掉了模型客户端 —— 连接池没有被复用"
    )
