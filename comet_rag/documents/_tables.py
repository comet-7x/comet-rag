from __future__ import annotations

from .schemas import ParsedDocument, TableContent


def table_dimensions(table: TableContent) -> tuple[int, int]:
    cells = table.cells or ()
    return (
        table.rows
        if table.rows is not None
        else max((cell.row + cell.row_span for cell in cells), default=0),
        table.columns
        if table.columns is not None
        else max((cell.column + cell.column_span for cell in cells), default=0),
    )


def validate_table_expansion(document: ParsedDocument, max_table_cells: int) -> None:
    if type(max_table_cells) is not int:
        raise ValueError("max_table_cells must be a positive integer")
    if max_table_cells <= 0:
        raise ValueError("max_table_cells must be a positive integer")
    positions = 0
    for block in document.blocks:
        if block.table is None:
            continue
        if block.table.cells is None:
            continue
        rows, columns = table_dimensions(block.table)
        # 零列表格仍逐行输出；预算同时覆盖空行和稀疏网格。
        positions += rows * max(columns, 1)
        if positions > max_table_cells:
            raise ValueError("table expansion exceeds cell budget")
