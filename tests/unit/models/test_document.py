from __future__ import annotations

from pathlib import Path

import pytest

from comet_rag.ports import (
    DocumentExtractionError,
    DocumentExtractorPort,
    DocumentProtocolError,
    DocumentResourceLimitExceeded,
    DocumentUpstreamError,
    ExtractedDocument,
    RetryableDocumentUpstreamError,
)
from tests.contracts.document_extractor import DocumentExtractorContract


class FakeDocumentExtractor:
    """只验证 Port 行为，不替未来 MinerU 适配器预演传输细节。"""

    def __init__(self, result: ExtractedDocument) -> None:
        self.result = result
        self.closed = False
        self.calls: list[tuple[Path, str, str]] = []

    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls.append((path, filename, media_type))
        return self.result

    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls.append((path, filename, media_type))
        return self.result

    async def aclose(self) -> None:
        self.closed = True


class TestFakeDocumentExtractor(DocumentExtractorContract):
    @pytest.fixture
    def expected_document(self) -> ExtractedDocument:
        return ExtractedDocument(markdown="# 标题\n\n正文", metadata={"page_count": 1})

    @pytest.fixture
    def extractor(self, expected_document: ExtractedDocument) -> FakeDocumentExtractor:
        return FakeDocumentExtractor(expected_document)

    @pytest.fixture
    def document_path(self, tmp_path: Path) -> Path:
        return tmp_path / "sample.pdf"


def test_fake_satisfies_document_extractor_protocol() -> None:
    extractor = FakeDocumentExtractor(ExtractedDocument(markdown="正文"))

    assert isinstance(extractor, DocumentExtractorPort)


def test_extracted_document_metadata_is_not_shared() -> None:
    first = ExtractedDocument(markdown="一")
    second = ExtractedDocument(markdown="二")

    first.metadata["source"] = "first"

    assert second.metadata == {}


def test_error_taxonomy_exposes_retry_boundary() -> None:
    assert issubclass(DocumentProtocolError, DocumentExtractionError)
    assert issubclass(DocumentResourceLimitExceeded, DocumentExtractionError)
    assert issubclass(DocumentUpstreamError, DocumentExtractionError)
    assert issubclass(RetryableDocumentUpstreamError, DocumentUpstreamError)
    assert not issubclass(DocumentProtocolError, RetryableDocumentUpstreamError)
    assert not issubclass(DocumentResourceLimitExceeded, RetryableDocumentUpstreamError)
