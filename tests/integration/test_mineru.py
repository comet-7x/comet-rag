from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import httpx
import pytest

from comet_rag.documents.parsers.mineru import AsyncMinerUParser
from comet_rag.documents.projection import project_document
from comet_rag.documents.schemas import SourceInfo
from comet_rag.sources.types import LocalResource
from tests.fixtures.pdf import minimal_pdf_bytes

pytestmark = pytest.mark.integration


def test_official_mineru_268_http_returns_text_with_explicit_partial_report(
    tmp_path: Path,
) -> None:
    endpoint = os.environ["COMET_RAG_TEST_MINERU_URL"]
    data = minimal_pdf_bytes()
    path = tmp_path / "document.pdf"
    path.write_bytes(data)
    resource = LocalResource(
        path,
        SourceInfo("mineru-integration-pdf", hashlib.sha256(data).hexdigest()),
        original_name="document.pdf",
        media_type="application/pdf",
        size_bytes=len(data),
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=10), trust_env=False
        ) as client:
            parser = AsyncMinerUParser(
                client=client, endpoint=endpoint, expected_version="2.6.8"
            )
            result = await parser.parse(resource, output_dir=tmp_path / "output")
            assert result.document.parsing.version == "2.6.8"
            assert result.document.parsing.config["backend"] == "pipeline"
            assert result.document.parsing.config["parse_method"] == "auto"
            assert "Hello PDF" in project_document(result.document).text
            assert (
                len(result.document.blocks) == 1
                and result.document.blocks[0].kind == "text"
            )
            assert result.document.pages == () and result.document.assets == ()
            assert result.report.status == "partial" and result.report.coverage is None
            assert [issue.code for issue in result.report.issues] == [
                "mineru_markdown_only"
            ]
            assert not client.is_closed and path.read_bytes() == data

    asyncio.run(run())
