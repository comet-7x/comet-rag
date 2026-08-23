"""入库用例：三阶段流水线、断点续跑、幂等重入库。

全程用假模型 + `InMemoryVectorStore` + `InMemoryTaskStore`，不碰任何中间件 ——
这正是 plan 里"先内存后真实"的意义：整条链路在零依赖下就能验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.infrastructure.knowledge_base import InMemoryKnowledgeBaseRepository
from comet_rag.infrastructure.providers.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from comet_rag.services.ingestion import (
    INGEST_KIND,
    IngestRunner,
    register_ingest_runner,
)
from comet_rag.services.knowledge_base import KnowledgeBaseService, KnowledgeBaseSpec
from comet_rag.tasks import (
    InMemoryTaskStore,
    InProcessExecutor,
    TaskService,
    TaskStatus,
)
from comet_rag.tasks.runner import unregister
from tests.contracts.support import wait_for_terminal

DIM = 3
STUB_TYPE = "stub"
KB = "kb-test"
MODEL = "fake-embed"


# ── 测试替身 ───────────────────────────────────────────────────────────────


class FakeEmbeddingModel(BaseEmbeddingModel):
    """确定性伪向量：同样的文本永远得到同样的向量，断言才稳。"""

    def __init__(self) -> None:
        self.calls = 0
        self.fail_times = 0
        self.failure: Exception | None = None

    def _vector(self, text: str) -> list[float]:
        return [float(len(text)), float(sum(map(ord, text)) % 97), 1.0]

    def _embed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        return self._vector(str(data))

    async def _aembed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.failure or RuntimeError("注入的失败")
        return self._vector(str(data))

    async def close_client(self) -> None:  # pragma: no cover
        return None


class StubLoader(BaseLoader):
    def __init__(self, path: Path, file_type: str = STUB_TYPE) -> None:
        self._path = path
        self.file_type = file_type
        self.loads = 0
        self.fail_times = 0
        self.failure: Exception | None = None

    def _load(self, source: SourceContent | str, *args, **kwargs) -> LoaderContent:
        self.loads += 1
        if not isinstance(source, SourceContent):
            source = SourceContent(str(source))
        return LoaderContent(
            path=self._path,
            source=source,
            is_temp=False,
            metadata={"file_type": self.file_type, "file_name": self._path.name},
        )

    async def _aload(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderContent:
        if self.fail_times > 0:
            self.loads += 1
            self.fail_times -= 1
            raise self.failure or RuntimeError("注入的下载失败")
        return self._load(source)

    def cleanup(self) -> None:
        return None


# ── 夹具 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def calls() -> dict[str, int]:
    return {"extract": 0, "chunk": 0}


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.stub"
    path.write_text("占位", encoding="utf-8")
    return path


@pytest.fixture
def loader(source_file: Path) -> StubLoader:
    return StubLoader(source_file)


@pytest.fixture(autouse=True)
def stub_hooks(calls: dict[str, int]) -> Iterator[None]:
    """注册 stub 格式的 hook。conftest 的 autouse 夹具会在用例后还原（T11）。"""

    @PipelineHooks.extractor(STUB_TYPE)
    def _extract(lc: LoaderContent, config: PipelineConfig) -> str:
        calls["extract"] += 1
        return "段落一。段落二。段落三。"

    @PipelineHooks.chunker(STUB_TYPE)
    def _chunk(text: str, config: PipelineConfig) -> list[str]:
        calls["chunk"] += 1
        return ["段落一。", "段落二。", "段落三。"]

    yield


@pytest.fixture
def model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel()


@pytest.fixture
def store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.fixture
def task_store() -> InMemoryTaskStore:
    return InMemoryTaskStore()


@pytest.fixture
def kb_service(store: InMemoryVectorStore) -> KnowledgeBaseService:
    return KnowledgeBaseService(
        repository=InMemoryKnowledgeBaseRepository(),
        vector_store=store,
        embedding_model=MODEL,
        embedding_dim=DIM,
    )


@pytest.fixture
async def svc(
    task_store: InMemoryTaskStore,
    model: FakeEmbeddingModel,
    store: InMemoryVectorStore,
    loader: StubLoader,
    kb_service: KnowledgeBaseService,
) -> AsyncIterator[TaskService]:
    # 入库前知识库必须存在（T19 起 IngestRunner 会做一致性检查）
    await kb_service.create(KnowledgeBaseSpec(kb_id=KB))
    register_ingest_runner(
        IngestRunner(
            embedding_model=model,
            vector_store=store,
            knowledge_base=kb_service,
            loader=loader,
            config=PipelineConfig(embed_batch_size=2, max_concurrency=4),
        )
    )
    executor = InProcessExecutor(task_store, max_concurrency=4, retry_backoff=0.01)
    yield TaskService(task_store, executor)
    await executor.shutdown(timeout=5.0)
    unregister(INGEST_KIND)


def request(**overrides: Any) -> dict[str, Any]:
    return {"kb_id": KB, "source": "任意来源", **overrides}


# ── 正常路径 ───────────────────────────────────────────────────────────────


async def test_ingest_writes_all_chunks(
    svc: TaskService, store: InMemoryVectorStore
) -> None:
    task = await svc.submit(INGEST_KIND, request())

    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.SUCCEEDED, done.error
    assert done.result["chunk_count"] == 3
    assert done.result["kb_id"] == KB
    assert await store.acount(KB) == 3


async def test_every_chunk_carries_kb_id(
    svc: TaskService, store: InMemoryVectorStore
) -> None:
    """租户维度必须落到每一条向量上（spec A5）—— 事后补要重灌数据。"""
    task = await svc.submit(INGEST_KIND, request())
    await wait_for_terminal(svc.store, task.task_id)

    hits = await store.asearch(KB, [1.0, 1.0, 1.0], top_k=10)

    assert all(h.metadata["kb_id"] == KB for h in hits)
    assert all("source_id" in h.metadata for h in hits)
    assert sorted(h.metadata["chunk_index"] for h in hits) == [0, 1, 2]


async def test_extra_metadata_is_attached(
    svc: TaskService, store: InMemoryVectorStore
) -> None:
    task = await svc.submit(
        INGEST_KIND, request(metadata={"department": "研发", "year": 2026})
    )
    await wait_for_terminal(svc.store, task.task_id)

    hit = (await store.asearch(KB, [1.0, 1.0, 1.0]))[0]

    assert hit.metadata["department"] == "研发"
    assert hit.metadata["year"] == 2026


async def test_stage_history_records_three_stages(svc: TaskService) -> None:
    task = await svc.submit(INGEST_KIND, request())
    done = await wait_for_terminal(svc.store, task.task_id)

    assert [r.stage for r in done.stage_history] == [
        "extracting",
        "chunking",
        "indexing",
    ]
    assert all(r.status == "succeeded" for r in done.stage_history)


async def test_large_intermediate_state_is_cleared(svc: TaskService) -> None:
    """文本与 chunk 用完即清 —— 否则每个任务行都拖着一份文档全文。"""
    task = await svc.submit(INGEST_KIND, request())
    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.context.get("text") is None
    assert done.context.get("chunks") is None
    assert done.context["source_id"], "但溯源信息要留着"


# ── 断点续跑（spec A10-修正）───────────────────────────────────────────────


async def test_retry_resumes_from_indexing_without_reparsing(
    svc: TaskService, model: FakeEmbeddingModel, calls: dict[str, int]
) -> None:
    """**本文件最重要的一条**。

    embedding 阶段因模型服务抖动失败时，不该把已经花掉 CPU 的解析和分块
    整个重做一遍 —— 那正是 spec A10-修正要解决的问题。
    """
    model.fail_times = 1
    model.failure = httpx.ConnectTimeout("模型服务超时")

    task = await svc.submit(INGEST_KIND, request(), max_attempts=3)
    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.SUCCEEDED, done.error
    assert done.attempts == 2
    assert calls["extract"] == 1, "解析被重跑了，说明续跑没生效"
    assert calls["chunk"] == 1, "分块被重跑了，说明续跑没生效"


async def test_failed_stage_is_recorded_as_failed(
    svc: TaskService, model: FakeEmbeddingModel
) -> None:
    model.fail_times = 1
    model.failure = httpx.ConnectTimeout("模型服务超时")

    task = await svc.submit(INGEST_KIND, request(), max_attempts=3)
    done = await wait_for_terminal(svc.store, task.task_id)

    statuses = [(r.stage, r.status) for r in done.stage_history]
    assert ("indexing", "failed") in statuses
    assert ("indexing", "succeeded") in statuses


# ── 错误分类 ───────────────────────────────────────────────────────────────


async def test_network_error_is_retriable(
    svc: TaskService, model: FakeEmbeddingModel
) -> None:
    model.fail_times = 99
    model.failure = httpx.ConnectTimeout("一直超时")

    task = await svc.submit(INGEST_KIND, request(), max_attempts=2)
    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.FAILED
    assert done.attempts == 2, "可重试错误应当用满重试次数"
    assert done.error is not None
    assert done.error.retriable is True


async def test_download_network_error_retries_extracting_stage(
    svc: TaskService, loader: StubLoader
) -> None:
    loader.fail_times = 1
    loader.failure = httpx.ConnectTimeout("下载超时")

    task = await svc.submit(INGEST_KIND, request(), max_attempts=3)
    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.SUCCEEDED
    assert done.attempts == 2
    assert loader.loads == 2


async def test_server_5xx_is_retriable(
    svc: TaskService, model: FakeEmbeddingModel
) -> None:
    model.fail_times = 1
    model.failure = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("POST", "http://x/v1/embeddings"),
        response=httpx.Response(503),
    )

    task = await svc.submit(INGEST_KIND, request(), max_attempts=3)
    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.SUCCEEDED
    assert done.attempts == 2


async def test_client_4xx_is_not_retriable(
    svc: TaskService, model: FakeEmbeddingModel
) -> None:
    """4xx 是"你请求得不对"，重试一万次也还是不对。"""
    model.fail_times = 99
    model.failure = httpx.HTTPStatusError(
        "bad request",
        request=httpx.Request("POST", "http://x/v1/embeddings"),
        response=httpx.Response(400),
    )

    task = await svc.submit(INGEST_KIND, request(), max_attempts=5)
    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.FAILED
    assert done.attempts == 1, "确定性错误不该消耗重试预算"
    assert done.error is not None
    assert done.error.retriable is False


async def test_unsupported_file_type_fails_fast(
    svc: TaskService, loader: StubLoader
) -> None:
    """没注册 extractor 的格式是配置问题，一次判死不进重试。"""
    loader.file_type = "从未注册过的格式"

    task = await svc.submit(INGEST_KIND, request(), max_attempts=3)
    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.FAILED
    assert done.attempts == 1, "确定性错误不该消耗重试预算"
    assert done.error is not None
    assert done.error.retriable is False


# ── 幂等与重入库 ───────────────────────────────────────────────────────────


async def test_reingest_replaces_old_chunks(
    svc: TaskService, store: InMemoryVectorStore
) -> None:
    """同一文档重新入库必须替换而非堆副本 —— chunk id 由 source_id 派生。"""
    first = await svc.submit(INGEST_KIND, request())
    await wait_for_terminal(svc.store, first.task_id)
    ids_before = {r["id"] for r in store.snapshot()[KB]}

    second = await svc.submit(INGEST_KIND, request())
    await wait_for_terminal(svc.store, second.task_id)

    assert await store.acount(KB) == 3
    assert {r["id"] for r in store.snapshot()[KB]} == ids_before


async def test_reingest_removes_chunks_that_no_longer_exist(
    svc: TaskService, store: InMemoryVectorStore, calls: dict[str, int]
) -> None:
    """新版本切得更少时，旧版本尾部的 chunk 必须被删掉。

    不删的话它们会永远留在库里 —— 内容早已不存在于文档中，却仍会被检索到。
    """
    task = await svc.submit(INGEST_KIND, request())
    await wait_for_terminal(svc.store, task.task_id)
    assert await store.acount(KB) == 3

    with PipelineHooks.temporary():

        @PipelineHooks.chunker(STUB_TYPE)
        def _fewer(text: str, config: PipelineConfig) -> list[str]:
            return ["只剩一块。"]

        again = await svc.submit(INGEST_KIND, request())
        await wait_for_terminal(svc.store, again.task_id)

    assert await store.acount(KB) == 1


async def test_reingest_never_leaves_the_document_missing(
    svc: TaskService, store: InMemoryVectorStore
) -> None:
    """**替换过程中，这份文档必须始终查得到**（PR 评审 #3）。

    "先删掉旧的全部块、再写新的"是天然写法，但删完到写完之间存在一个窗口，
    期间这份文档在库里**根本不存在**。任务在这中间失败、被取消、或 worker
    崩掉，用户就永久失去了原本好好的那一版 —— 而他只想更新一下。

    做法是在每次写入后都数一遍，全程不得归零。上面两条用例只看**结果**
    （替换掉了、尾巴清了），看不见中间这个空窗。
    """
    first = await svc.submit(INGEST_KIND, request())
    await wait_for_terminal(svc.store, first.task_id)
    assert await store.acount(KB) == 3

    # ⚠️ 必须在**每一次改动之后**采样，不能只盯 upsert。
    # 第一版只包了 upsert，于是"先删后写"里那次删除完全没被看见 ——
    # 反向验证时用例照样绿，等于没测。
    seen: list[int] = []
    original_upsert, original_delete = store.aupsert, store.adelete

    async def watching_upsert(kb_id, records):
        result = await original_upsert(kb_id, records)
        seen.append(await store.acount(kb_id))
        return result

    async def watching_delete(kb_id, **kwargs):
        result = await original_delete(kb_id, **kwargs)
        seen.append(await store.acount(kb_id))
        return result

    store.aupsert = watching_upsert  # type: ignore[method-assign]
    store.adelete = watching_delete  # type: ignore[method-assign]
    try:
        with PipelineHooks.temporary():

            @PipelineHooks.chunker(STUB_TYPE)
            def _fewer(text: str, config: PipelineConfig) -> list[str]:
                return ["只剩一块。"]

            again = await svc.submit(INGEST_KIND, request())
            await wait_for_terminal(svc.store, again.task_id)
    finally:
        store.aupsert = original_upsert  # type: ignore[method-assign]
        store.adelete = original_delete  # type: ignore[method-assign]

    assert seen, "第二次入库一次都没动过向量库 —— 用例没测到东西"
    assert all(count > 0 for count in seen), (
        f"替换过程中文档一度消失（每次改动后的块数：{seen}）—— 说明是先删后写"
    )
    assert await store.acount(KB) == 1, "旧版本的尾巴没清掉，会变成查得到的幽灵"


async def test_idempotency_key_prevents_duplicate_work(
    svc: TaskService, calls: dict[str, int]
) -> None:
    first = await svc.submit(INGEST_KIND, request(), idempotency_key="doc-1")
    again = await svc.submit(INGEST_KIND, request(), idempotency_key="doc-1")
    await wait_for_terminal(svc.store, first.task_id)

    assert again.task_id == first.task_id
    assert calls["extract"] == 1


async def test_chunk_ids_are_stable_across_runs(
    svc: TaskService, store: InMemoryVectorStore
) -> None:
    """id 不稳定的话，重新入库就会产生副本而不是覆盖。"""
    task = await svc.submit(INGEST_KIND, request())
    await wait_for_terminal(svc.store, task.task_id)
    first_ids = sorted(r["id"] for r in store.snapshot()[KB])

    await store.adrop_collection(KB)
    task2 = await svc.submit(INGEST_KIND, request())
    await wait_for_terminal(svc.store, task2.task_id)

    assert sorted(r["id"] for r in store.snapshot()[KB]) == first_ids


# ── 取消 ───────────────────────────────────────────────────────────────────


async def test_cancel_during_indexing(
    svc: TaskService, model: FakeEmbeddingModel
) -> None:
    """indexing 每个窗口前都有 checkpoint，取消才能及时生效。"""
    original = model.aembed

    async def slow(data, **kwargs):
        await asyncio.sleep(0.05)
        return await original(data, **kwargs)

    model.aembed = slow  # type: ignore[method-assign]

    task = await svc.submit(INGEST_KIND, request())
    await asyncio.sleep(0.06)
    await svc.cancel(task.task_id)

    done = await wait_for_terminal(svc.store, task.task_id)
    assert done.status in (TaskStatus.CANCELLED, TaskStatus.SUCCEEDED)


# ── 请求校验 ───────────────────────────────────────────────────────────────


async def test_invalid_request_fails_without_retry(svc: TaskService) -> None:
    task = await svc.submit(INGEST_KIND, {"kb_id": "", "source": ""}, max_attempts=3)

    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.FAILED
    assert done.attempts == 1


async def test_ingest_into_unknown_kb_fails_fast(svc: TaskService) -> None:
    """往不存在的知识库灌数据是确定性错误，一次判死不进重试（spec A12）。"""
    task = await svc.submit(INGEST_KIND, request(kb_id="从未建过"), max_attempts=3)

    done = await wait_for_terminal(svc.store, task.task_id)

    assert done.status is TaskStatus.FAILED
    assert done.attempts == 1
    assert done.error is not None
    assert done.error.retriable is False
