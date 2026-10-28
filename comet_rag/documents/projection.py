"""投影 v1：块间换行，格间制表符；合成字符没有来源块。"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import Block, FormulaContent, InlineFormula, ParsedDocument


@dataclass(frozen=True, slots=True)
class ProjectionSpan:
    start: int
    end: int
    block_id: str | None
    block_start: int | None = None
    block_end: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectionLimitation:
    block_id: str
    code: str
    offset: int
    asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class TextProjection:
    document_id: str
    projection_version: int
    text: str
    spans: tuple[ProjectionSpan, ...]
    limitations: tuple[ProjectionLimitation, ...]


def project_document(document: ParsedDocument) -> TextProjection:
    """偏移按 Python 字符计数；图片公式保留零宽诊断而不伪造字符。"""
    blocks = {block.block_id: block for block in document.blocks}
    cell_members = {
        member
        for block in document.blocks
        if block.table is not None
        for cell in block.table.cells or ()
        for member in cell.block_ids
    }
    pieces: list[str] = []
    spans: list[ProjectionSpan] = []
    limitations: list[ProjectionLimitation] = []
    position = 0

    def append(
        text: str, block_id: str | None = None, local: int | None = None
    ) -> None:
        nonlocal position
        if text:
            pieces.append(text)
            spans.append(
                ProjectionSpan(
                    position,
                    position + len(text),
                    block_id,
                    local,
                    local + len(text) if local is not None else None,
                )
            )
            position += len(text)

    def formula(content: FormulaContent, block_id: str, local: int) -> int:
        if content.expression is None:
            limitations.append(
                ProjectionLimitation(
                    block_id, "image_only_formula", local, content.asset_id
                )
            )
            return local
        append(content.expression, block_id, local)
        return local + len(content.expression)

    def emit(block: Block) -> None:
        if block.rich_text is not None:
            local = 0
            for inline in block.rich_text.inlines:
                if isinstance(inline, InlineFormula):
                    local = formula(inline.formula, block.block_id, local)
                else:
                    append(inline.text, block.block_id, local)
                    local += len(inline.text)
        elif block.code is not None:
            append(block.code, block.block_id, 0)
        elif block.formula is not None:
            formula(block.formula, block.block_id, 0)
        elif block.asset_id is not None:
            # alt_text 属于这次图片出现；不会自动生成图片描述。
            if block.alt_text is None:
                limitations.append(
                    ProjectionLimitation(
                        block.block_id, "image_without_text", 0, block.asset_id
                    )
                )
            else:
                append(block.alt_text, block.block_id, 0)
        elif block.table is not None:
            table = block.table
            if table.cells is None:
                limitations.append(
                    ProjectionLimitation(
                        block.block_id,
                        f"table_{table.representation}_not_textualized",
                        0,
                        table.asset_id,
                    )
                )
                return
            cells = {(cell.row, cell.column): cell for cell in table.cells}
            rows = (
                table.rows
                if table.rows is not None
                else max((cell.row + cell.row_span for cell in table.cells), default=0)
            )
            columns = (
                table.columns
                if table.columns is not None
                else max(
                    (cell.column + cell.column_span for cell in table.cells), default=0
                )
            )
            if (
                sum(cell.row_span * cell.column_span for cell in table.cells)
                < rows * columns
            ):
                limitations.append(
                    ProjectionLimitation(block.block_id, "table_unknown_cells", 0)
                )
            # 合并区域只在左上角输出一次；空格位的分隔符始终是合成字符。
            for row in range(rows):
                if row:
                    append("\n")
                for column in range(columns):
                    if column:
                        append("\t")
                    cell = cells.get((row, column))
                    if cell is not None:
                        for i, member in enumerate(cell.block_ids):
                            if i:
                                append("\n")
                            emit(blocks[member])

    for i, block in enumerate(
        block for block in document.blocks if block.block_id not in cell_members
    ):
        if i:
            append("\n")
        emit(block)
    return TextProjection(
        document.document_id,
        document.projection_version,
        "".join(pieces),
        tuple(spans),
        tuple(limitations),
    )
