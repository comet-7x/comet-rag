from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from comet_rag.infrastructure.providers.document import (
    MINERU_API_PROTOCOL_VERSION,
    MinerUDocumentExtractor,
)
from comet_rag.ports import (
    DocumentExtractorPort,
    DocumentProtocolError,
    DocumentUpstreamError,
    ExtractedDocument,
)
from tests.contracts.document_extractor import DocumentExtractorContract

BASE_URL = "https://mineru.test/api"
MARKDOWN = "# 标题\n\n正文包含 $E=mc^2$。"


class ScriptedMinerU:
    def __init__(
        self,
        *,
        statuses: tuple[str, ...] = ("pending", "processing", "completed"),
        health: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        submission: dict[str, Any] | None = None,
    ) -> None:
        self.statuses = statuses
        self.health = (
            health
            if health is not None
            else {
                "status": "healthy",
                "version": "3.3.0",
                "protocol_version": MINERU_API_PROTOCOL_VERSION,
            }
        )
        self.result = (
            result
            if result is not None
            else {
                "backend": "pipeline",
                "version": "3.3.0",
                "results": {"sample": {"md_content": MARKDOWN}},
            }
        )
        self.submission = submission
        self.requests: list[httpx.Request] = []
        self.submission_bodies: list[bytes] = []
        self.submissions = 0
        self.polls: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/health"):
            return httpx.Response(200, json=self.health)
        if request.method == "POST" and path.endswith("/tasks"):
            self.submissions += 1
            self.submission_bodies.append(request.content)
            task_id = f"task-{self.submissions}"
            payload = (
                self.submission
                if self.submission is not None
                else {
                    "task_id": task_id,
                    "status": "pending",
                    # 适配器不得跟随服务端返回的任意地址。
                    "status_url": f"https://attacker.invalid/tasks/{task_id}",
                    "result_url": f"https://attacker.invalid/tasks/{task_id}/result",
                }
            )
            return httpx.Response(202, json=payload)
        if request.method == "GET" and path.endswith("/result"):
            return httpx.Response(200, json=self.result)
        if request.method == "GET" and "/tasks/" in path:
            task_id = path.rsplit("/", 1)[-1]
            index = self.polls.get(task_id, 0)
            status = self.statuses[min(index, len(self.statuses) - 1)]
            self.polls[task_id] = index + 1
            return httpx.Response(
                200,
                json={
                    "task_id": task_id,
                    "status": status,
                    "error": "模型执行失败" if status == "failed" else None,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})


def _extractor(
    server: ScriptedMinerU,
    *,
    sleeps: list[float] | None = None,
    async_sleeps: list[float] | None = None,
) -> MinerUDocumentExtractor:
    transport = httpx.MockTransport(server)
    sync_client = httpx.Client(transport=transport)
    async_client = httpx.AsyncClient(transport=transport)

    def sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    async def asleep(delay: float) -> None:
        if async_sleeps is not None:
            async_sleeps.append(delay)

    return MinerUDocumentExtractor(
        BASE_URL,
        poll_interval_seconds=0.25,
        sync_client=sync_client,
        async_client=async_client,
        sleep=sleep,
        asleep=asleep,
    )


@pytest.fixture
def document_path(tmp_path: Path) -> Path:
    return _write_pdf(tmp_path)


def _write_pdf(directory: Path) -> Path:
    path = directory / "sample.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture")
    return path


class TestMinerUDocumentExtractor(DocumentExtractorContract):
    @pytest.fixture
    def extractor(self) -> DocumentExtractorPort:
        return _extractor(ScriptedMinerU())

    @pytest.fixture
    def document_path(self, tmp_path: Path) -> Path:
        return _write_pdf(tmp_path)

    @pytest.fixture
    def expected_document(self) -> ExtractedDocument:
        return ExtractedDocument(
            markdown=MARKDOWN,
            metadata={
                "provider": "mineru",
                "backend": "pipeline",
                "version": "3.3.0",
                "protocol_version": MINERU_API_PROTOCOL_VERSION,
            },
        )


async def test_sync_and_async_paths_handle_queued_states(
    document_path: Path,
) -> None:
    server = ScriptedMinerU()
    sync_sleeps: list[float] = []
    async_sleeps: list[float] = []
    extractor = _extractor(server, sleeps=sync_sleeps, async_sleeps=async_sleeps)

    sync_result = extractor.extract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    async_result = await extractor.aextract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )

    assert sync_result == async_result
    assert sync_sleeps == [0.25, 0.25]
    assert async_sleeps == [0.25, 0.25]
    assert all(request.url.host == "mineru.test" for request in server.requests)


def test_submission_streams_one_pdf_and_sends_every_output_flag(
    document_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ScriptedMinerU(statuses=("completed",))
    extractor = _extractor(server)

    def forbidden_read_bytes(self: Path) -> bytes:
        raise AssertionError("上传不得一次性 read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    extractor.extract(
        document_path, filename="original.pdf", media_type="application/pdf"
    )

    body = server.submission_bodies[0]
    assert b'name="files"; filename="original.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert b"%PDF-1.7\nfixture" in body
    expected = {
        "lang_list": "ch",
        "backend": "pipeline",
        "parse_method": "auto",
        "formula_enable": "true",
        "table_enable": "true",
        "return_md": "true",
        "return_middle_json": "false",
        "return_model_output": "false",
        "return_content_list": "false",
        "return_images": "false",
        "response_format_zip": "false",
        "return_original_file": "false",
        "client_side_output_generation": "false",
    }
    for field, value in expected.items():
        fragment = f'name="{field}"\r\n\r\n{value}\r\n'.encode()
        assert fragment in body


@pytest.mark.parametrize(
    ("health", "message"),
    [
        (
            {"status": "unhealthy", "version": "3.3.0", "protocol_version": 2},
            "healthy",
        ),
        (
            {"status": "healthy", "version": "3.3.0", "protocol_version": 1},
            "protocol_version",
        ),
        ({"status": "healthy", "protocol_version": 2}, "version"),
    ],
)
def test_health_contract_is_validated(
    document_path: Path, health: dict[str, Any], message: str
) -> None:
    extractor = _extractor(ScriptedMinerU(health=health))
    error = (
        DocumentUpstreamError
        if health["status"] == "unhealthy"
        else DocumentProtocolError
    )

    with pytest.raises(error, match=message):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


def test_failed_task_surfaces_remote_error(document_path: Path) -> None:
    extractor = _extractor(ScriptedMinerU(statuses=("failed",)))

    with pytest.raises(DocumentUpstreamError, match="模型执行失败"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


@pytest.mark.parametrize(
    "result",
    [
        {"results": {}},
        {"results": {"one": {"md_content": "一"}, "two": {"md_content": "二"}}},
        {"results": {"sample": {}}},
        {"results": {"sample": "不是 object"}},
    ],
)
def test_result_requires_exactly_one_markdown(
    document_path: Path, result: dict[str, Any]
) -> None:
    extractor = _extractor(ScriptedMinerU(statuses=("completed",), result=result))

    with pytest.raises(DocumentProtocolError):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


@pytest.mark.parametrize("status", ["cancelled", "unknown", ""])
def test_unknown_task_status_is_protocol_error(
    document_path: Path, status: str
) -> None:
    extractor = _extractor(ScriptedMinerU(statuses=(status,)))

    with pytest.raises(DocumentProtocolError, match="未知任务状态"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


def test_submission_requires_non_empty_task_id(document_path: Path) -> None:
    extractor = _extractor(
        ScriptedMinerU(submission={"task_id": "", "status": "pending"})
    )

    with pytest.raises(DocumentProtocolError, match="task_id"):
        extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )


async def test_clients_are_reused_and_injected_clients_remain_open(
    document_path: Path,
) -> None:
    server = ScriptedMinerU(statuses=("completed",))
    extractor = _extractor(server)
    sync_client = extractor.sync_client
    async_client = extractor.async_client

    extractor.extract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    extractor.extract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    await extractor.aextract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    await extractor.aextract(
        document_path, filename="sample.pdf", media_type="application/pdf"
    )
    await extractor.aclose()

    assert extractor.sync_client is sync_client
    assert extractor.async_client is async_client
    assert server.submissions == 4
    assert not sync_client.is_closed
    assert not async_client.is_closed
    sync_client.close()
    await async_client.aclose()


async def test_internal_clients_are_closed_idempotently() -> None:
    extractor = MinerUDocumentExtractor(BASE_URL)

    await extractor.aclose()
    await extractor.aclose()

    assert extractor.sync_client.is_closed
    assert extractor.async_client.is_closed


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_url": "mineru.test"}, "HTTP"),
        ({"base_url": BASE_URL, "parse_method": "magic"}, "parse_method"),
        ({"base_url": BASE_URL, "poll_interval_seconds": -1}, "poll_interval"),
    ],
)
def test_constructor_rejects_invalid_wire_configuration(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MinerUDocumentExtractor(**kwargs)
