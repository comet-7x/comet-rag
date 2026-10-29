from __future__ import annotations

from unittest.mock import patch

import pytest

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.documents.exporters.markdown import export_markdown
from comet_rag.documents.projection import project_document
from comet_rag.documents.schemas import Block, TableCell, TableContent
from tests.unit.test_chunkers import document


@pytest.mark.parametrize("render", [project_document, export_markdown])
@pytest.mark.parametrize(
    "table",
    [
        TableContent(cells=(), rows=10**9, columns=10**9),
        TableContent(cells=(), rows=10**9, columns=0),
        TableContent(cells=(TableCell(10**9, 0),)),
    ],
)
def test_sparse_tables_fail_before_grid_expansion(render, table: TableContent) -> None:
    doc = document(Block("table", "table", table=table))
    with pytest.raises(ValueError, match="table.*budget"):
        render(doc)


@pytest.mark.parametrize("render", [project_document, export_markdown])
@pytest.mark.parametrize("limit", [True, 0, -1, 1.5])
def test_table_expansion_budget_rejects_invalid_limits(render, limit) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        render(document(), max_table_cells=limit)


@pytest.mark.parametrize("render", [project_document, export_markdown])
def test_table_expansion_budget_is_shared_across_blocks(render) -> None:
    doc = document(
        Block("one", "table", table=TableContent(cells=(), rows=2, columns=3)),
        Block("two", "table", table=TableContent(cells=(), rows=2, columns=3)),
    )
    render(doc, max_table_cells=12)
    with pytest.raises(ValueError, match="table.*budget"):
        render(doc, max_table_cells=11)


def test_chunker_rejects_sparse_atomic_table_before_projection() -> None:
    doc = document(
        Block("table", "table", table=TableContent(cells=(), rows=1, columns=1000))
    )
    with patch("comet_rag.chunkers.document.hybrid.project_document") as project:
        with pytest.raises(ValueError, match="atomic table.*chunk_size"):
            DocumentChunker(RecursiveTextChunker(4)).chunk(doc)
        project.assert_not_called()


@pytest.mark.parametrize("render", [project_document, export_markdown])
def test_table_expansion_budget_allows_exact_boundary(render) -> None:
    doc = document(
        Block("table", "table", table=TableContent(cells=(), rows=2, columns=3))
    )
    render(doc, max_table_cells=6)
    with pytest.raises(ValueError, match="table.*budget"):
        render(doc, max_table_cells=5)
