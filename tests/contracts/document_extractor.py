from __future__ import annotations

from pathlib import Path

import pytest

from comet_rag.ports import DocumentExtractorPort, ExtractedDocument


class DocumentExtractorContract:
    """实现方提供 extractor、document_path 与 expected_document fixture。"""

    @pytest.fixture
    def extractor(self) -> DocumentExtractorPort:  # pragma: no cover - 由实现方提供
        raise NotImplementedError("实现方必须提供 extractor fixture")

    @pytest.fixture
    def document_path(self) -> Path:  # pragma: no cover - 由实现方提供
        raise NotImplementedError("实现方必须提供 document_path fixture")

    @pytest.fixture
    def expected_document(self) -> ExtractedDocument:  # pragma: no cover
        raise NotImplementedError("实现方必须提供 expected_document fixture")

    def test_sync_extract_returns_normalized_document(
        self,
        extractor: DocumentExtractorPort,
        document_path: Path,
        expected_document: ExtractedDocument,
    ) -> None:
        result = extractor.extract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )

        assert result == expected_document

    async def test_async_extract_matches_sync_semantics(
        self,
        extractor: DocumentExtractorPort,
        document_path: Path,
        expected_document: ExtractedDocument,
    ) -> None:
        result = await extractor.aextract(
            document_path, filename="sample.pdf", media_type="application/pdf"
        )

        assert result == expected_document

    async def test_close_is_idempotent(self, extractor: DocumentExtractorPort) -> None:
        await extractor.aclose()
        await extractor.aclose()
