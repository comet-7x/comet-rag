from __future__ import annotations

from comet_rag.engines.chunkers.protocol import Chunker
from comet_rag.engines.chunkers.strategies._blocks import split_blocks
from comet_rag.engines.chunkers.strategies.errors import (
    DocumentStructureError,
    MissingDocumentStructureError,
)
from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.ports.document import DocumentBlock, NormalizedDocument


class MarkdownSectionStrategy:
    """在标题 section 硬边界内调用原子 Chunker。"""

    def __init__(self, chunker: Chunker, /) -> None:
        self._chunker = chunker

    def split(self, document: NormalizedDocument, /) -> list[ChunkDraft]:
        if not document.blocks:
            raise MissingDocumentStructureError("文档没有 Markdown section 结构")
        if any(block.kind != "section" for block in document.blocks):
            raise DocumentStructureError(
                "MarkdownSectionStrategy 只能消费 section blocks"
            )
        return split_blocks(
            document,
            document.blocks,
            self._chunker,
            self._metadata,
        )

    @staticmethod
    def _metadata(block: DocumentBlock) -> dict[str, object]:
        return {
            "block_kind": "section",
            "heading_path": list(block.heading_path),
        }


__all__ = ["MarkdownSectionStrategy"]
