from __future__ import annotations

from pathlib import Path

import pytest

from comet_rag.models import (
    ImageContent,
    MediaResource,
    RankedDocument,
    RerankDocument,
    TextContent,
)


def test_media_resource_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="必须且只能"):
        MediaResource()
    with pytest.raises(ValueError, match="必须且只能"):
        MediaResource(path=Path("image.png"), url="https://example.com/image.png")


def test_byte_resource_requires_mimetype() -> None:
    with pytest.raises(ValueError, match="mimetype"):
        MediaResource(data=b"image")


def test_rerank_document_freezes_content_sequence() -> None:
    parts = [
        TextContent("一只猫"),
        ImageContent(MediaResource(path=Path("cat.png"))),
    ]

    document = RerankDocument(id="cat", content=parts)

    parts.clear()
    assert len(document.content) == 2


def test_ranked_document_keeps_original_candidate() -> None:
    document = RerankDocument(id="doc-1", content="正文")

    result = RankedDocument(index=2, score=0.9, document=document)

    assert result.document.id == "doc-1"
