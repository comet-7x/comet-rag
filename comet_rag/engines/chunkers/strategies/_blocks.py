from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from comet_rag.engines.chunkers.protocol import Chunker
from comet_rag.engines.chunkers.strategies.errors import DocumentStructureError
from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.ports.document import DocumentBlock, NormalizedDocument

BlockMetadata = Callable[[DocumentBlock], Mapping[str, object]]


def split_blocks(
    document: NormalizedDocument,
    blocks: Sequence[DocumentBlock],
    chunker: Chunker,
    metadata_for: BlockMetadata,
) -> list[ChunkDraft]:
    """逐结构块调用原子 Chunker，并把局部 span 映射回整篇文档。"""

    result: list[ChunkDraft] = []
    for block in blocks:
        source = document.markdown[block.start_char : block.end_char]
        drafts = chunker.split(source)
        if not isinstance(drafts, list):
            raise DocumentStructureError("Chunker 必须返回 list[ChunkDraft]")
        for expected, draft in enumerate(drafts):
            local_start, local_end = _validate_local_draft(
                draft, expected=expected, source=source
            )
            start = block.start_char + local_start
            end = block.start_char + local_end
            metadata = {
                **block.metadata,
                **draft.metadata,
                **metadata_for(block),
            }
            result.append(
                ChunkDraft(
                    text=document.markdown[start:end],
                    ordinal=len(result),
                    start_char=start,
                    end_char=end,
                    metadata=metadata,
                )
            )
    return result


def _validate_local_draft(
    draft: object,
    *,
    expected: int,
    source: str,
) -> tuple[int, int]:
    if not isinstance(draft, ChunkDraft):
        raise DocumentStructureError(
            f"Chunker 第 {expected} 项必须是 ChunkDraft"
        )
    if draft.ordinal != expected:
        raise DocumentStructureError("Chunker ordinal 必须从 0 严格连续")
    if draft.start_char is None or draft.end_char is None:
        raise DocumentStructureError("文档级策略要求 Chunker 返回字符 span")
    if draft.end_char > len(source):
        raise DocumentStructureError("Chunker span 超出当前 DocumentBlock")
    if source[draft.start_char : draft.end_char] != draft.text:
        raise DocumentStructureError("Chunker text 与字符 span 不一致")
    return draft.start_char, draft.end_char


__all__ = []
