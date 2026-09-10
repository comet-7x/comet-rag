"""真实 MinerU：文本/扫描 PDF 的完整任务链路与资源指标。"""

from __future__ import annotations

import asyncio
import json
import os
import time
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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
    MinerUConfig,
    MinerUParseMethod,
    ServerConfig,
)
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.providers.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.vectorstore import InMemoryVectorStore
from comet_rag.tasks import Task, TaskStatus
from tests.contracts.support import wait_for_terminal
from tests.fixtures.pdf import build_minimal_pdf, build_scanned_pdf

pytestmark = pytest.mark.integration

DIM = 3
KB = "kb-mineru-integration"


class ConstantEmbedding(BaseEmbeddingModel):
    """真实测试只替换模型服务，避免把 MinerU 验收绑定到另一块 GPU。"""

    def _embed(self, data: str, **kwargs: Any) -> list[float]:
        return [1.0, 0.0, 0.0]

    async def _aembed(self, data: str, **kwargs: Any) -> list[float]:
        return [1.0, 0.0, 0.0]


@dataclass(frozen=True, slots=True)
class SampleMetric:
    sample: str
    source_bytes: int
    markdown_bytes: int
    chunks: int
    elapsed_seconds: float
    extracting_seconds: float
    chunking_seconds: float
    indexing_seconds: float
    cpu_lane_seconds: float
    cpu_lane_ratio: float


async def _mineru_url() -> str:
    value = os.environ.get("COMET_TEST_MINERU_URL", "").strip().rstrip("/")
    if not value:
        pytest.skip("未设置 COMET_TEST_MINERU_URL，跳过真实 MinerU 集成测试")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{value}/health")
    except httpx.RequestError as exc:
        pytest.skip(f"COMET_TEST_MINERU_URL 不可达，跳过：{exc}")
    if response.status_code >= 500:
        pytest.skip(f"MinerU 当前不可用（health={response.status_code}），跳过")
    if response.status_code != 200:
        pytest.fail(
            f"COMET_TEST_MINERU_URL 配置错误：GET /health 返回 {response.status_code}",
            pytrace=False,
        )
    return value


def _config(tmp_path: Path, base_url: str) -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(
            app_name="comet-rag-mineru-integration", host="127.0.0.1", port=0
        ),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="integration-embed", dim=DIM
            ),
            mineru=MinerUConfig(
                enabled=True,
                base_url=base_url,
                backend=os.environ.get("COMET_TEST_MINERU_BACKEND", "pipeline"),
                parse_method=MinerUParseMethod(
                    os.environ.get("COMET_TEST_MINERU_PARSE_METHOD", "auto")
                ),
                language=os.environ.get("COMET_TEST_MINERU_LANGUAGE", "ch"),
            ),
        ),
        ingest_policy=IngestPolicyConfig(
            allow_local=True,
            local_roots=[str(tmp_path)],
        ),
    )


def _stage_seconds(task: Task, stage: str) -> float:
    records = [record for record in task.stage_history if record.stage == stage]
    assert len(records) == 1, f"{stage} 应恰好执行一次，实际 {len(records)}"
    record = records[0]
    assert record.finished_at is not None
    return (record.finished_at - record.started_at).total_seconds()


def _metric(name: str, source: Path, task: Task, elapsed: float) -> SampleMetric:
    extracting = _stage_seconds(task, "extracting")
    chunking = _stage_seconds(task, "chunking")
    indexing = _stage_seconds(task, "indexing")
    cpu_lane = extracting + chunking
    markdown_bytes = task.context.get("extracted_text_bytes")
    assert isinstance(markdown_bytes, int) and markdown_bytes > 0
    return SampleMetric(
        sample=name,
        source_bytes=source.stat().st_size,
        markdown_bytes=markdown_bytes,
        chunks=task.result["chunk_count"],
        elapsed_seconds=elapsed,
        extracting_seconds=extracting,
        chunking_seconds=chunking,
        indexing_seconds=indexing,
        cpu_lane_seconds=cpu_lane,
        cpu_lane_ratio=cpu_lane / elapsed if elapsed else 0.0,
    )


async def _ingest(
    client: httpx.AsyncClient,
    context: Any,
    name: str,
    source: Path,
    timeout: float,
) -> tuple[SampleMetric, Task]:
    started = time.perf_counter()
    submitted = await client.post(
        "/ingest",
        json={"kb_id": KB, "source": str(source), "max_attempts": 1},
    )
    assert submitted.status_code == 202, submitted.text
    task = await wait_for_terminal(
        context.task_store,
        submitted.json()["task_id"],
        timeout=timeout,
    )
    elapsed = time.perf_counter() - started
    assert task.status is TaskStatus.SUCCEEDED, task.error
    assert task.result["chunk_count"] > 0
    return _metric(name, source, task, elapsed), task


def _report(
    config: APPConfig,
    samples: list[SampleMetric],
    *,
    client_peak_heap_bytes: int,
    mineru_peak_in_flight: int,
) -> dict[str, Any]:
    mineru = config.infrastructure_config.mineru
    max_elapsed = max(sample.elapsed_seconds for sample in samples)
    max_markdown = max(sample.markdown_bytes for sample in samples)
    return {
        "meta": {
            "timestamp": datetime.now(UTC).isoformat(),
            "note": (
                "heap 仅指 Comet-RAG 测试进程；cpu_lane_seconds 是 extracting + "
                "chunking 的 wall time，不含外部 MinerU 进程的 CPU/GPU/RSS"
            ),
        },
        "configuration": {
            "mineru_concurrency": config.limits.mineru_concurrency,
            "parse_timeout_seconds": mineru.parse_timeout_seconds,
            "max_response_bytes": mineru.max_response_bytes,
            "max_markdown_bytes": mineru.max_markdown_bytes,
        },
        "observed": {
            "client_peak_heap_bytes": client_peak_heap_bytes,
            "mineru_peak_in_flight": mineru_peak_in_flight,
            "max_elapsed_budget_ratio": max_elapsed / mineru.parse_timeout_seconds,
            "max_markdown_budget_ratio": max_markdown / mineru.max_markdown_bytes,
        },
        "samples": [asdict(sample) for sample in samples],
    }


async def test_real_mineru_text_and_scanned_pdf_task_chain(
    tmp_path: Path,
    pytestconfig: pytest.Config,
) -> None:
    """只通过公开 API 提交，通过 TaskStore 等终态，不窥探执行器协程。"""
    base_url = await _mineru_url()
    text_pdf = build_minimal_pdf(tmp_path / "text.pdf")
    scanned_pdf = build_scanned_pdf(tmp_path / "scanned.pdf")
    config = _config(tmp_path, base_url)
    app = create_app(
        config,
        embedding_model=ConstantEmbedding(),
        vector_store=InMemoryVectorStore(),
        pipeline_config=PipelineConfig(chunk_size=512, chunk_overlap=0),
    )
    transport = httpx.ASGITransport(app=app)
    timeout = config.infrastructure_config.mineru.parse_timeout_seconds + 30.0

    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            created = await client.post("/kb", json={"kb_id": KB})
            assert created.status_code == 201, created.text
            measured = await asyncio.gather(
                _ingest(client, app.state.ctx, "text", text_pdf, timeout),
                _ingest(client, app.state.ctx, "scanned", scanned_pdf, timeout),
            )
            samples = [item[0] for item in measured]

            for source, tokens in (
                (text_pdf, ("HELLO", "PDF")),
                (scanned_pdf, ("SCAN", "TEST", "42")),
            ):
                found = await client.post(
                    "/search",
                    json={
                        "kb_id": KB,
                        "query": " ".join(tokens),
                        "top_k": 10,
                        "filter": {"source": str(source)},
                    },
                )
                assert found.status_code == 200, found.text
                chunks = found.json()["chunks"]
                assert chunks, f"{source.name} 已入库但检索不到任何 chunk"
                extracted = "\n".join(chunk["text"] for chunk in chunks).upper()
                assert any(token in extracted for token in tokens)

            _, peak_heap = tracemalloc.get_traced_memory()
            gate_stats = app.state.ctx.mineru_gate.stats
            report = _report(
                config,
                samples,
                client_peak_heap_bytes=peak_heap,
                mineru_peak_in_flight=gate_stats.peak_in_flight,
            )
    finally:
        if not already_tracing:
            tracemalloc.stop()

    report_path = Path(str(pytestconfig.getoption("--mineru-report")))
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert (
        report["observed"]["mineru_peak_in_flight"] <= config.limits.mineru_concurrency
    )
