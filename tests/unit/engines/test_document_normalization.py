"""跨格式文档规范化策略。"""

from __future__ import annotations

import pytest

from comet_rag.engines.documents.normalization import (
    DocumentNormalizationStrategy,
    MarkdownDocumentNormalizer,
)
from comet_rag.ports import DocumentBlock, ExtractedDocument, NormalizedDocument


def test_normalizes_shared_markdown_representation() -> None:
    normalizer = MarkdownDocumentNormalizer()
    document = ExtractedDocument(
        markdown="\ufeffCafe\u0301\r\n\r\n\r\nA\u00a0B  \r尾部\x00\r\n",
        metadata={"provider": "fixture"},
    )

    result = normalizer.normalize(document)

    assert result == NormalizedDocument(
        markdown="Café\n\nA B\n尾部",
        metadata={"provider": "fixture"},
        blocks=(DocumentBlock("section", 0, 0, 12),),
    )


def test_preserves_fenced_code_content() -> None:
    document = ExtractedDocument(
        markdown="说明  \n```python\nx = 1  \n\n\n```\n\n结尾\t\n"
    )

    result = MarkdownDocumentNormalizer().normalize(document)

    assert result.markdown == "说明\n```python\nx = 1  \n\n\n```\n\n结尾"


def test_normalization_is_idempotent_and_does_not_alias_metadata() -> None:
    metadata: dict[str, object] = {"page_count": 2}
    normalizer = MarkdownDocumentNormalizer()

    first = normalizer.normalize(
        ExtractedDocument(markdown="\n正文  \n\n\n", metadata=metadata)
    )
    second = normalizer.normalize(
        ExtractedDocument(markdown=first.markdown, metadata=first.metadata)
    )
    metadata["page_count"] = 3

    assert first == second
    assert first.metadata == {"page_count": 2}


def test_normalizer_satisfies_strategy_protocol() -> None:
    assert isinstance(MarkdownDocumentNormalizer(), DocumentNormalizationStrategy)


def test_rejects_legacy_string_hook_result_with_helpful_error() -> None:
    with pytest.raises(TypeError, match="must return ExtractedDocument, got str"):
        MarkdownDocumentNormalizer().normalize("正文")  # type: ignore[arg-type]


def test_rejects_non_string_markdown_with_helpful_error() -> None:
    document = ExtractedDocument(markdown=123)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="markdown must be str, got int"):
        MarkdownDocumentNormalizer().normalize(document)
