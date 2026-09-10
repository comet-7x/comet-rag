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

    @pytest.fixture
    def filename(self) -> str:
        return "sample.pdf"

    @pytest.fixture
    def media_type(self) -> str:
        return "application/pdf"

    def test_sync_extract_returns_normalized_document(
        self,
        extractor: DocumentExtractorPort,
        document_path: Path,
        expected_document: ExtractedDocument,
        filename: str,
        media_type: str,
    ) -> None:
        result = extractor.extract(
            document_path, filename=filename, media_type=media_type
        )

        assert result.markdown == expected_document.markdown
        assert result.metadata == expected_document.metadata

    async def test_async_extract_matches_sync_semantics(
        self,
        extractor: DocumentExtractorPort,
        document_path: Path,
        expected_document: ExtractedDocument,
        filename: str,
        media_type: str,
    ) -> None:
        result = await extractor.aextract(
            document_path, filename=filename, media_type=media_type
        )

        assert result.markdown == expected_document.markdown
        assert result.metadata == expected_document.metadata

    async def test_close_is_idempotent(self, extractor: DocumentExtractorPort) -> None:
        await extractor.aclose()
        await extractor.aclose()
