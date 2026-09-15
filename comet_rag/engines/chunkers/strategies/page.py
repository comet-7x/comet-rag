from __future__ import annotations

from comet_rag.engines.chunkers.protocol import Chunker
from comet_rag.engines.chunkers.strategies._blocks import split_blocks
from comet_rag.engines.chunkers.strategies.errors import (
    DocumentStructureError,
    MissingDocumentStructureError,
)
from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.ports.document import DocumentBlock, NormalizedDocument


class PageChunkingStrategy:
    """按真实页面硬边界分块；缺少页事实时拒绝猜测。"""

    def __init__(self, chunker: Chunker, /) -> None:
        self._chunker = chunker

    def split(self, document: NormalizedDocument, /) -> list[ChunkDraft]:
        if not document.blocks:
            raise MissingDocumentStructureError("文档没有 page 结构")
        if any(block.kind != "page" for block in document.blocks):
            raise DocumentStructureError(
                "PageChunkingStrategy 只能消费 page blocks"
            )
        previous_page = 0
        for block in document.blocks:
            if block.page_number is None:
                raise DocumentStructureError("page block 必须提供 page_number")
            if block.page_number <= previous_page:
                raise DocumentStructureError("page_number 必须严格递增")
            previous_page = block.page_number
        return split_blocks(
            document,
            document.blocks,
            self._chunker,
            self._metadata,
        )

    @staticmethod
    def _metadata(block: DocumentBlock) -> dict[str, object]:
        if block.page_number is None:
            raise DocumentStructureError("page block 必须提供 page_number")
        metadata: dict[str, object] = {
            "block_kind": "page",
            "page_number": block.page_number,
        }
        if block.heading_path:
            metadata["heading_path"] = list(block.heading_path)
        return metadata


__all__ = ["PageChunkingStrategy"]
