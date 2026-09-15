from __future__ import annotations

import re
import unicodedata

from comet_rag.engines.documents.markdown import MarkdownStructureAnalyzer
from comet_rag.ports.document import (
    DocumentBlock,
    ExtractedDocument,
    NormalizedDocument,
)

_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<rest>.*)$")


class MarkdownDocumentNormalizer:
    """保守规范化 Markdown，同时保持 fenced code block 的内部内容。"""

    def normalize(self, document: ExtractedDocument) -> NormalizedDocument:
        if not isinstance(document, ExtractedDocument):
            raise TypeError(
                "Document extractor must return ExtractedDocument, got "
                f"{type(document).__name__}"
            )
        if not isinstance(document.markdown, str):
            raise TypeError(
                "ExtractedDocument.markdown must be str, got "
                f"{type(document.markdown).__name__}"
            )
        normalized = self._normalize_markdown(document.markdown)
        page_blocks = self._page_blocks(document, normalized)
        return NormalizedDocument(
            markdown=normalized,
            metadata=dict(document.metadata),
            blocks=(
                page_blocks
                if page_blocks is not None
                else MarkdownStructureAnalyzer().analyze(normalized)
            ),
        )

    def _page_blocks(
        self, document: ExtractedDocument, normalized: str
    ) -> tuple[DocumentBlock, ...] | None:
        """只接受可由页片段精确重建的页边界，拒绝文本搜索猜位置。"""
        if not document.pages:
            return None

        fragments: list[tuple[int, str, dict[str, object]]] = []
        for page in document.pages:
            fragment = self._normalize_markdown(page.markdown)
            if fragment:
                fragments.append(
                    (page.page_number, fragment, dict(page.metadata))
                )

        rebuilt = "\n\n".join(fragment for _, fragment, _ in fragments)
        if not fragments or rebuilt != normalized:
            return None

        blocks: list[DocumentBlock] = []
        cursor = 0
        for ordinal, (page_number, fragment, metadata) in enumerate(fragments):
            end = cursor + len(fragment)
            blocks.append(
                DocumentBlock(
                    kind="page",
                    ordinal=ordinal,
                    start_char=cursor,
                    end_char=end,
                    page_number=page_number,
                    metadata=metadata,
                )
            )
            cursor = end + 2
        return tuple(blocks)

    @classmethod
    def _normalize_markdown(cls, markdown: str) -> str:
        canonical = unicodedata.normalize(
            "NFC",
            markdown.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\ufeff", "")
            .replace("\x00", ""),
        )
        return cls._normalize_lines(canonical)

    @staticmethod
    def _normalize_lines(markdown: str) -> str:
        lines: list[str] = []
        fence_char: str | None = None
        fence_length = 0

        for raw_line in markdown.split("\n"):
            match = _FENCE.match(raw_line)
            marker = match.group("marker") if match else ""

            if fence_char is not None:
                lines.append(raw_line)
                if (
                    marker.startswith(fence_char)
                    and len(marker) >= fence_length
                    and match is not None
                    and not match.group("rest").strip()
                ):
                    fence_char = None
                    fence_length = 0
                continue

            line = raw_line.replace("\u00a0", " ").rstrip(" \t")
            if match is not None:
                fence_char = marker[0]
                fence_length = len(marker)
                lines.append(line)
                continue

            if line:
                lines.append(line)
            elif lines and lines[-1] != "":
                lines.append("")

        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


__all__ = ["MarkdownDocumentNormalizer"]
