from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from comet_rag.documents._tables import table_dimensions
from comet_rag.documents.projection import TextProjection, project_document
from comet_rag.documents.schemas import Block, InlineFormula, ParsedDocument

from ..text.recursive import RecursiveTextChunker
from ..types import Chunk, SourceRef


def _block_ranges(
    document: ParsedDocument, projection: TextProjection
) -> list[tuple[Block, int, int]]:
    blocks = {block.block_id: block for block in document.blocks}
    lengths: dict[str, int] = {}
    for span in projection.spans:
        if span.block_id is not None:
            lengths[span.block_id] = (
                lengths.get(span.block_id, 0) + span.end - span.start
            )
    cell_members = {
        member
        for block in document.blocks
        if block.table is not None
        for cell in block.table.cells or ()
        for member in cell.block_ids
    }

    def width(block: Block) -> int:
        table = block.table
        if table is None or table.cells is None:
            return lengths.get(block.block_id, 0)
        rows, columns = table_dimensions(table)
        # 投影只给叶块范围；这里只计 v1 合成分隔符，不重新渲染正文。
        return (
            max(rows - 1, 0)
            + rows * max(columns - 1, 0)
            + sum(
                max(len(cell.block_ids) - 1, 0)
                + sum(width(blocks[member]) for member in cell.block_ids)
                for cell in table.cells
            )
        )

    result: list[tuple[Block, int, int]] = []
    position = 0
    for block in document.blocks:
        if block.block_id in cell_members:
            continue
        if result:
            position += 1
        end = position + width(block)
        result.append((block, position, end))
        position = end
    if position != len(projection.text):
        raise ValueError("unsupported document projection layout")
    return result


def _atomic(block: Block) -> bool:
    return block.kind in {"image", "table", "formula"} or (
        block.rich_text is not None
        and any(
            isinstance(inline, InlineFormula) and inline.formula.asset_id is not None
            for inline in block.rich_text.inlines
        )
    )


def _source_refs(
    projection: TextProjection, span_ends: tuple[int, ...], start: int, end: int
) -> tuple[SourceRef, ...]:
    refs: list[SourceRef] = []
    index = bisect_right(span_ends, start)
    while index < len(projection.spans):
        span = projection.spans[index]
        if span.start >= end:
            break
        if span.block_id is not None:
            if span.block_start is None:
                raise ValueError("source projection span requires a block offset")
            local_start = span.block_start + max(start - span.start, 0)
            local_end = span.block_start + min(end, span.end) - span.start
            if (
                refs
                and refs[-1].block_id == span.block_id
                and refs[-1].end == local_start
            ):
                previous = refs.pop()
                local_start = previous.start
            refs.append(
                SourceRef(
                    projection.document_id,
                    span.block_id,
                    projection.projection_version,
                    local_start,
                    local_end,
                )
            )
        index += 1
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class DocumentChunker:
    """普通块共享投影切片；原子块独立选择，省略其块间及纯合成片段。"""

    text_chunker: RecursiveTextChunker

    def __post_init__(self) -> None:
        if not isinstance(self.text_chunker, RecursiveTextChunker):
            raise TypeError("text_chunker must be a RecursiveTextChunker")

    @property
    def config(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "type": "document",
                "text_chunker": self.text_chunker.config,
                "atomic_overflow": "reject",
                "atomic_join_separators": "omit",
                "synthetic_only": "omit",
            }
        )

    def chunk(self, document: ParsedDocument) -> tuple[Chunk, ...]:
        if not isinstance(document, ParsedDocument):
            raise TypeError("document must be a ParsedDocument")
        for block in document.blocks:
            if block.table is not None and block.table.cells is not None:
                rows, columns = table_dimensions(block.table)
                separators = max(rows - 1, 0) + rows * max(columns - 1, 0)
                if separators > self.text_chunker.chunk_size:
                    raise ValueError(
                        f"atomic table block {block.block_id!r} exceeds "
                        f"chunk_size={self.text_chunker.chunk_size}: "
                        f"{separators} characters in table separators"
                    )
        projection = project_document(document)
        span_ends = tuple(span.end for span in projection.spans)
        chunks: list[Chunk] = []
        pending_start: int | None = None
        pending_end = 0

        def flush() -> None:
            if pending_start is None:
                return
            text = projection.text[pending_start:pending_end]
            for span in self.text_chunker.split(text):
                refs = _source_refs(
                    projection,
                    span_ends,
                    pending_start + span.start,
                    pending_start + span.end,
                )
                if refs:
                    chunks.append(
                        Chunk(
                            document.document_id,
                            text[span.start : span.end],
                            refs,
                            len(chunks),
                        )
                    )

        for block, start, end in _block_ranges(document, projection):
            if not _atomic(block):
                if pending_start is None:
                    pending_start = start
                pending_end = end
                continue
            flush()
            pending_start = None
            if end - start > self.text_chunker.chunk_size:
                raise ValueError(
                    f"atomic {block.kind} block {block.block_id!r} exceeds "
                    f"chunk_size={self.text_chunker.chunk_size}: {end - start} characters"
                )
            chunks.append(
                Chunk(
                    document.document_id,
                    projection.text[start:end],
                    (
                        SourceRef(
                            document.document_id,
                            block.block_id,
                            document.projection_version,
                        ),
                    ),
                    len(chunks),
                )
            )
        flush()
        return tuple(chunks)
