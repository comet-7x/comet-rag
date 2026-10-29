from __future__ import annotations

from dataclasses import replace
from typing import Literal

from comet_rag.chunkers import DocumentChunker, SourceRef
from comet_rag.content import ImagePart, TextPart
from comet_rag.documents.schemas import (
    Block,
    InlineCode,
    InlineFormula,
    ParsedDocument,
    RichText,
    TextRun,
)
from comet_rag.embedder.types import EmbeddingSpace
from comet_rag.identity import stable_id

from ..types import BuildResult, IndexUnit, Representation


def _selected_block(block: Block, refs: tuple[SourceRef, ...]) -> Block:
    if not refs or any(ref.start is None for ref in refs):
        return block
    if block.code is not None:
        return replace(
            block, code="".join(block.code[ref.start : ref.end] for ref in refs)
        )
    if block.rich_text is None:
        return block
    inlines: list[TextRun | InlineCode | InlineFormula] = []
    position = 0
    for inline in block.rich_text.inlines:
        text = (
            inline.formula.expression or ""
            if isinstance(inline, InlineFormula)
            else inline.text
        )
        end = position + len(text)
        for ref in refs:
            if ref.start is None or ref.end is None:
                continue
            start, stop = max(ref.start, position), min(ref.end, end)
            if start < stop:
                # 公式源码不可截成另一条公式；原文选择仍由 source_refs 精确限定。
                inlines.append(
                    inline
                    if isinstance(inline, InlineFormula)
                    else replace(inline, text=text[start - position : stop - position])
                )
        position = end
    return replace(block, rich_text=RichText(tuple(inlines)))


class FlatIndexBuilder:
    def __init__(
        self,
        chunker: DocumentChunker,
        *,
        representation: Literal["text", "media", "joint", "separate"] = "text",
    ) -> None:
        if representation not in ("text", "media", "joint", "separate"):
            raise ValueError("unsupported representation selection")
        self.chunker = chunker
        self.representation = representation

    def profile_id(self, space: EmbeddingSpace) -> str:
        return stable_id(
            "profile",
            {
                "builder": "flat_v3",
                "chunker": self.chunker.config,
                "representations": self.representation,
                "space": space.space_id,
            },
        )

    def build(
        self, document: ParsedDocument, *, index_scope: str, index_profile_id: str
    ) -> BuildResult:
        by_id = {block.block_id: block for block in document.blocks}
        asset_by_id = {asset.asset_id: asset for asset in document.assets}
        units = []
        representations = []
        for chunk in self.chunker.chunk(document):
            if not chunk.source_refs:
                continue
            refs_by_block: dict[str, list[SourceRef]] = {}
            for ref in chunk.source_refs:
                refs_by_block.setdefault(ref.block_id, []).append(ref)
            selected = set(refs_by_block)
            pending = list(selected)
            while pending:
                block = by_id[pending.pop()]
                if block.table is not None and block.table.cells is not None:
                    for cell in block.table.cells:
                        for child in cell.block_ids:
                            if child not in selected:
                                selected.add(child)
                                pending.append(child)
            blocks = tuple(
                _selected_block(block, tuple(refs_by_block.get(block.block_id, ())))
                for block in document.blocks
                if block.block_id in selected
            )
            media_ids: list[str] = []
            for block in blocks:
                values = [
                    block.asset_id,
                    block.formula.asset_id if block.formula else None,
                    block.table.asset_id if block.table else None,
                ]
                if block.rich_text is not None:
                    values.extend(
                        inline.formula.asset_id
                        for inline in block.rich_text.inlines
                        if isinstance(inline, InlineFormula)
                    )
                for asset_id in values:
                    if asset_id is not None:
                        media_ids.append(asset_id)
            # Asset 按身份保存一份，实际图片输入仍保留每次出现。
            asset_ids = list(dict.fromkeys(media_ids))
            # 派生资源的来源也随单元保存，但不把来源图片当作额外检索表示。
            for asset_id in asset_ids:
                for source_asset in asset_by_id[asset_id].source_asset_ids:
                    if source_asset not in asset_ids:
                        asset_ids.append(source_asset)
            assets = tuple(asset_by_id[asset_id] for asset_id in asset_ids)
            parts: list[TextPart | ImagePart] = (
                [TextPart(chunk.text)] if chunk.text else []
            )
            parts.extend(
                ImagePart(asset_by_id[asset_id].media) for asset_id in media_ids
            )
            if not parts:
                raise ValueError("selected content has no materializable text or media")
            unit = IndexUnit(
                index_scope,
                index_profile_id,
                document.source.source_id,
                document.document_id,
                tuple(parts),
                chunk.source_refs,
                ordinal=chunk.ordinal,
                assets=assets,
                locations=tuple(
                    location for block in blocks for location in block.locations
                ),
                blocks=blocks,
                metadata=document.metadata,
            )
            units.append(unit)
            variants: list[tuple[str, tuple[TextPart | ImagePart, ...]]] = []
            text = tuple(part for part in parts if isinstance(part, TextPart))
            media = tuple(part for part in parts if isinstance(part, ImagePart))
            if self.representation == "text":
                if not text:
                    raise ValueError(
                        "text representation cannot index content without a text view"
                    )
                variants.append(("original_text", text))
            elif self.representation == "media":
                if not media:
                    raise ValueError("media representation requires an actual image")
                variants.extend((f"image_{i}", (part,)) for i, part in enumerate(media))
            elif self.representation == "joint":
                variants.append(("original_joint", tuple(parts)))
            else:
                if text:
                    variants.append(("original_text", text))
                variants.extend((f"image_{i}", (part,)) for i, part in enumerate(media))
            for purpose, content in variants:
                representations.append(
                    Representation(
                        unit.unit_id,
                        index_scope,
                        index_profile_id,
                        unit.source_id,
                        unit.document_id,
                        content,
                        purpose=purpose,
                        source_refs=chunk.source_refs,
                        context_refs=chunk.context_refs,
                        assets=assets,
                    )
                )
        return BuildResult(tuple(units), tuple(representations))
