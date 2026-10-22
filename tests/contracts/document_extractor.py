from __future__ import annotations

from pathlib import Path

import pytest

from comet_rag.ports import (
    AsyncDocumentExtractorPort,
    DocumentExtractorPort,
    ExtractedDocument,
)


class _DocumentExpectations:
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


class DocumentExtractorContract(_DocumentExpectations):
    @pytest.fixture
    def extractor(self) -> DocumentExtractorPort:  # pragma: no cover - 由实现方提供
        raise NotImplementedError("实现方必须提供 extractor fixture")

    def test_sync_extract_returns_extracted_document(
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

    def test_close_is_idempotent(self, extractor: DocumentExtractorPort) -> None:
        extractor.close()
        extractor.close()


class AsyncDocumentExtractorContract(_DocumentExpectations):
    @pytest.fixture
    def async_extractor(
        self,
    ) -> AsyncDocumentExtractorPort:  # pragma: no cover - 由实现方提供
        raise NotImplementedError("实现方必须提供 async_extractor fixture")

    async def test_async_extract_matches_sync_semantics(
        self,
        async_extractor: AsyncDocumentExtractorPort,
        document_path: Path,
        expected_document: ExtractedDocument,
        filename: str,
        media_type: str,
    ) -> None:
        result = await async_extractor.extract(
            document_path, filename=filename, media_type=media_type
        )

        assert result.markdown == expected_document.markdown
        assert result.metadata == expected_document.metadata

    async def test_async_close_is_idempotent(
        self, async_extractor: AsyncDocumentExtractorPort
    ) -> None:
        await async_extractor.close()
        await async_extractor.close()
