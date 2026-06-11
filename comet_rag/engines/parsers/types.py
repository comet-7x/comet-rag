from dataclasses import dataclass, field
from typing import Any


@dataclass
class BaseParsedContent:
    metadata: dict[str, Any]

    @property
    def text(self) -> Any:
        return ""


Block = dict[str, Any]


@dataclass
class DocxParsedContent(BaseParsedContent):
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        def _extract(b: "Block") -> str:
            t = b.get("type", "")
            if t in (
                "text",
                "heading",
                "caption",
                "equation",
                "table",
                "header",
                "footer",
            ):
                return b.get("content", "")
            if t == "image":
                alt = b.get("alt_text") or b.get("name") or b.get("id") or ""
                return f"![{alt}]" if alt else ""
            if t == "list":
                parts = [_extract(item) for item in b.get("content", [])]
                return "\n".join(p for p in parts if p)
            return ""

        parts = [_extract(b) for b in self.blocks]
        return "\n\n".join(p for p in parts if p)
