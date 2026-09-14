from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from docx.document import Document as DocumentObject

Block = dict[str, Any]


@dataclass(slots=True)
class DocxDocument:
    """python-docx 文档及其来源元数据。"""

    elements: DocumentObject
    metadata: dict[str, Any]


@dataclass(slots=True)
class DocxParsedContent:
    """DOCX 解析阶段产生的块列表。"""

    metadata: dict[str, Any]
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        """将解析块投影为纯文本。"""

        def _extract(block: Block) -> str:
            block_type = block.get("type", "")
            if block_type in (
                "text",
                "heading",
                "caption",
                "equation",
                "table",
                "header",
                "footer",
            ):
                return block.get("content", "")
            if block_type == "image":
                alt = (
                    block.get("alt_text")
                    or block.get("name")
                    or block.get("id")
                    or ""
                )
                return f"![{alt}]" if alt else ""
            if block_type == "list":
                parts = [_extract(item) for item in (block.get("content") or [])]
                return "\n".join(part for part in parts if part)
            return ""

        parts = [_extract(block) for block in self.blocks]
        return "\n\n".join(part for part in parts if part)


__all__ = ["Block", "DocxDocument", "DocxParsedContent"]
