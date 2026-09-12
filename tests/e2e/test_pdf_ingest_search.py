"""PDF 全链路：三种来源、任务恢复、取消与检索。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    ServerConfig,
)
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel
from comet_rag.infrastructure.persistence.vector_store import InMemoryVectorStore
from comet_rag.infrastructure.sources import (
    AutoLoader,
    LoaderRoute,
    LocalLoader,
    URLLoader,
)
from comet_rag.infrastructure.sources.s3 import S3Loader
from comet_rag.ports import ExtractedDocument, RetryableDocumentUpstreamError
from comet_rag.ports.gate import GatedResource
from comet_rag.tasks import InMemoryTaskStore, InProcessExecutor, TaskStatus
from tests.contracts.support import wait_for_terminal
from tests.fixtures.docx.build import build_basic
from tests.fixtures.pdf import build_minimal_pdf, minimal_pdf_bytes

pytestmark = pytest.mark.e2e

DIM = 4
KB = "kb-pdf-e2e"
URL_SOURCE = "https://documents.test/reports/remote.pdf"
S3_SOURCE = "s3://documents/reports/object.pdf"
PDF_MARKDOWN = """## 正文
正文检索词：MinerU 提取了这一段正文。

## 表格
| 项目 | 值 |
| --- | --- |
| 表格检索词 | 42 |

## 公式
公式检索词：$E = mc^2$。
"""
KEYWORDS = ("正文检索词", "表格检索词", "公式检索词", "第一段正文")


class KeywordEmbedding(BaseEmbeddingModel):
    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * DIM
        for index, keyword in enumerate(KEYWORDS):
            if keyword in text:
                vector[index] = 1.0
        return vector

    def _embed(self, data: str, **kwargs: Any) -> list[float]:
        return self._vector(data)

    async def _aembed(self, data: str, **kwargs: Any) -> list[float]:
        if self.fail_times:
            self.fail_times -= 1
            raise httpx.ConnectTimeout("注入的 embedding 超时")
        return self._vector(data)


class FakePdfExtractor(GatedResource):
    def __init__(self, *, fail_times: int = 0, block: bool = False) -> None:
        self.fail_times = fail_times
        self.block = block
        self.calls: list[tuple[Path, str, str]] = []
        self.started = asyncio.Event()
        self.cancelled = 0

    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls.append((path, filename, media_type))
        return ExtractedDocument(markdown=PDF_MARKDOWN)

    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls.append((path, filename, media_type))
        self.started.set()
        if self.fail_times:
            self.fail_times -= 1
            raise RetryableDocumentUpstreamError("注入的 MinerU 503")
        if self.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return ExtractedDocument(markdown=PDF_MARKDOWN)

    async def aclose(self) -> None:
        return None


class AsyncObjectBody:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False

    async def iter_chunks(self, *, chunk_size: int):
        assert chunk_size > 0
        yield self._content

    def close(self) -> None:
        self.closed = True


class AsyncS3Client:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Bucket": "documents", "Key": "reports/object.pdf"}
        return {"ContentLength": len(self._content), "ETag": '"pdf-etag"'}

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Bucket": "documents", "Key": "reports/object.pdf"}
        return {
            "ContentLength": len(self._content),
            "Body": AsyncObjectBody(self._content),
        }


def _config(tmp_path: Path) -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(
            app_name="comet-rag-pdf-e2e", host="127.0.0.1", port=0
        ),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="pdf-test-embed", dim=DIM
            ),
            mineru=MinerUConfig(enabled=True, base_url="https://mineru.test/api"),
        ),
        ingest_policy=IngestPolicyConfig(
            allow_local=True,
            local_roots=[str(tmp_path)],
            allow_private_network=True,
            allowed_url_hosts=["documents.test"],
            allow_s3=True,
            allowed_s3_buckets=["documents"],
        ),
    )


@asynccontextmanager
async def running_pdf_app(
    tmp_path: Path,
    extractor: FakePdfExtractor,
    *,
    embedding: KeywordEmbedding | None = None,
    chunk_size: int = 64,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    pdf = minimal_pdf_bytes()

    def download(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == URL_SOURCE
        return httpx.Response(200, content=pdf)

    url_client = httpx.AsyncClient(transport=httpx.MockTransport(download))
    url_dir = tmp_path / "url"
    s3_dir = tmp_path / "s3"
    url_dir.mkdir()
    s3_dir.mkdir()
    loader = AutoLoader(
        [
            LoaderRoute.schemes(
                "s3",
                S3Loader(
                    download_dir=s3_dir,
                    async_client=AsyncS3Client(pdf),
                ),
                {"s3", "minio"},
            ),
            LoaderRoute.local(LocalLoader()),
            LoaderRoute.schemes(
                "url",
                URLLoader(download_dir=url_dir, async_client=url_client),
                {"http", "https"},
            ),
        ]
    )
    task_store = InMemoryTaskStore()
    executor = InProcessExecutor(task_store, retry_backoff=0.01)
    app = create_app(
        _config(tmp_path),
        embedding_model=embedding or KeywordEmbedding(),
        vector_store=InMemoryVectorStore(),
        task_store=task_store,
        task_executor=executor,
        ingest_loader=loader,
        mineru_extractor=extractor,
        pipeline_config=PipelineConfig(
            chunk_size=chunk_size,
            chunk_overlap=0,
            embed_batch_size=2,
            max_concurrency=4,
        ),
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            yield client, app.state.ctx
    finally:
        await url_client.aclose()


async def _submit_and_wait(
    client: httpx.AsyncClient,
    context: Any,
    source: str,
    *,
    max_attempts: int = 1,
):
    response = await client.post(
        "/ingest",
        json={"kb_id": KB, "source": source, "max_attempts": max_attempts},
    )
    assert response.status_code == 202, response.text
    return await wait_for_terminal(context.task_store, response.json()["task_id"])


async def test_local_url_and_s3_pdf_use_one_port_and_are_searchable(
    tmp_path: Path,
) -> None:
    local = build_minimal_pdf(tmp_path / "local.pdf")
    extractor = FakePdfExtractor()
    async with running_pdf_app(tmp_path, extractor) as (client, context):
        assert (await client.post("/kb", json={"kb_id": KB})).status_code == 201

        completed = [
            await _submit_and_wait(client, context, source)
            for source in (str(local), URL_SOURCE, S3_SOURCE)
        ]

        assert all(task.status is TaskStatus.SUCCEEDED for task in completed)
        assert all(
            [record.stage for record in task.stage_history]
            == ["extracting", "chunking", "indexing"]
            for task in completed
        )
        assert len(extractor.calls) == 3
        assert {call[2] for call in extractor.calls} == {"application/pdf"}

        for keyword in KEYWORDS[:3]:
            found = await client.post(
                "/search", json={"kb_id": KB, "query": keyword, "top_k": 1}
            )
            assert found.status_code == 200, found.text
            assert keyword in found.json()["chunks"][0]["text"]


async def test_forged_local_pdf_fails_before_the_port_is_called(tmp_path: Path) -> None:
    forged = tmp_path / "forged.pdf"
    forged.write_text("plain text with a forged PDF suffix", encoding="utf-8")
    extractor = FakePdfExtractor()
    async with running_pdf_app(tmp_path, extractor) as (client, context):
        await client.post("/kb", json={"kb_id": KB})

        done = await _submit_and_wait(client, context, str(forged), max_attempts=3)

        assert done.status is TaskStatus.FAILED
        assert done.attempts == 1
        assert done.error is not None and "does not match" in done.error.message
        assert extractor.calls == []


async def test_pdf_retry_and_index_resume_are_observed_only_through_task_store(
    tmp_path: Path,
) -> None:
    local = build_minimal_pdf(tmp_path / "retry.pdf")
    extractor = FakePdfExtractor(fail_times=1)
    embedding = KeywordEmbedding(fail_times=1)
    async with running_pdf_app(
        tmp_path, extractor, embedding=embedding, chunk_size=2000
    ) as (client, context):
        await client.post("/kb", json={"kb_id": KB})

        done = await _submit_and_wait(client, context, str(local), max_attempts=3)

        assert done.status is TaskStatus.SUCCEEDED, done.error
        assert done.attempts == 3
        assert len(extractor.calls) == 2, "indexing 重试不得重新上传 PDF"
        assert [(record.stage, record.status) for record in done.stage_history] == [
            ("extracting", "failed"),
            ("extracting", "succeeded"),
            ("chunking", "succeeded"),
            ("indexing", "failed"),
            ("indexing", "succeeded"),
        ]


async def test_cancelling_pdf_extraction_releases_downloaded_file(
    tmp_path: Path,
) -> None:
    extractor = FakePdfExtractor(block=True)
    async with running_pdf_app(tmp_path, extractor) as (client, context):
        await client.post("/kb", json={"kb_id": KB})
        response = await client.post(
            "/ingest", json={"kb_id": KB, "source": URL_SOURCE}
        )
        task_id = response.json()["task_id"]
        await asyncio.wait_for(extractor.started.wait(), timeout=2.0)
        downloaded_path = extractor.calls[0][0]
        assert downloaded_path.exists()

        cancelled = await client.post(f"/tasks/{task_id}/cancel")
        assert cancelled.status_code == 202
        done = await wait_for_terminal(context.task_store, task_id)

        assert done.status is TaskStatus.CANCELLED
        assert extractor.cancelled == 1
        assert not downloaded_path.exists()


async def test_enabling_pdf_extraction_does_not_regress_docx_e2e(
    tmp_path: Path,
) -> None:
    docx = build_basic(tmp_path / "existing.docx")
    extractor = FakePdfExtractor()
    async with running_pdf_app(tmp_path, extractor, chunk_size=128) as (
        client,
        context,
    ):
        await client.post("/kb", json={"kb_id": KB})

        done = await _submit_and_wait(client, context, str(docx))
        found = await client.post(
            "/search", json={"kb_id": KB, "query": "第一段正文", "top_k": 1}
        )

        assert done.status is TaskStatus.SUCCEEDED, done.error
        assert done.result["file_type"] == "docx"
        assert found.status_code == 200, found.text
        assert "第一段正文" in found.json()["chunks"][0]["text"]
        assert extractor.calls == [], "DOCX 不得误路由到 PDF 外部提取器"
