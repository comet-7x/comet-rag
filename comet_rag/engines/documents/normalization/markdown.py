from __future__ import annotations

import re
import unicodedata

from comet_rag.ports.document import ExtractedDocument, NormalizedDocument

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
        markdown = unicodedata.normalize(
            "NFC",
            document.markdown.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\ufeff", "")
            .replace("\x00", ""),
        )
        return NormalizedDocument(
            markdown=self._normalize_lines(markdown),
            metadata=dict(document.metadata),
        )

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
