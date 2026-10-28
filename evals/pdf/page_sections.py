from __future__ import annotations

from comet_rag.engines.chunkers._coverage import validate_coverage
from comet_rag.engines.chunkers.protocol import Chunker
from comet_rag.engines.chunkers.strategies._blocks import split_blocks
from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.engines.documents.mineru.structured import StructuredProjection
from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer
from comet_rag.ports.document import DocumentBlock, NormalizedDocument


def page_sections(result: StructuredProjection) -> NormalizedDocument:
    """实验视图：页是账本事实，索引块是页内连续标题区间；不改canonical或现有page策略。"""
    # 合并后连续的大区间会掩盖账本内部漏项，必须先核对原始证据覆盖。
    validate_coverage(
        result.document.markdown, ((s.start_char, s.end_char) for s in result.evidence)
    )
    pages = MarkdownDocumentNormalizer().normalize(result.document)
    blocks: list[DocumentBlock] = []
    for page in pages.blocks:
        spans = [s for s in result.evidence if s.page_number == page.page_number]
        for span in spans:
            if not page.start_char <= span.start_char < span.end_char <= page.end_char:
                raise ValueError("证据区间跨越物理页")
            # 同页同标题且同类别才可合并；重复标题的不同出现不跨边界合并。
            same = (
                bool(blocks)
                and span.block_type not in {"doc_title", "paragraph_title"}
                and blocks[-1].page_number == span.page_number
                and blocks[-1].heading_path == span.heading_path
                and blocks[-1].metadata["evidence_category"] == span.category
            )
            if same:
                previous = blocks.pop()
                start = previous.start_char
            else:
                start = span.start_char
            blocks.append(
                DocumentBlock(
                    kind="page_section",
                    ordinal=len(blocks),
                    start_char=start,
                    end_char=span.end_char,
                    heading_path=span.heading_path,
                    page_number=span.page_number,
                    metadata={"evidence_category": span.category},
                )
            )
    return NormalizedDocument(result.document.markdown, blocks=tuple(blocks))


def split_page_sections(
    result: StructuredProjection, chunker: Chunker
) -> list[ChunkDraft]:
    """复用既有区间/覆盖守卫，验证复合边界；不提供新的生产ChunkingStrategy。"""
    view = page_sections(result)
    return split_blocks(
        view,
        view.blocks,
        chunker,
        lambda b: {
            "block_kind": "page_section",
            "page_number": b.page_number,
            "heading_path": list(b.heading_path),
        },
    )
