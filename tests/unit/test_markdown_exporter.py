from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path

import pytest

from comet_rag.content import MediaRef
from comet_rag.documents.exporters import export_markdown
from comet_rag.documents.parsers.docx import AsyncDocxParser
from comet_rag.documents.parsers.text import AsyncTextParser
from comet_rag.documents.schemas import (
    Asset,
    Block,
    FormulaContent,
    InlineCode,
    InlineFormula,
    PageInfo,
    ParsedDocument,
    ParsingInfo,
    RichText,
    SourceInfo,
    TableCell,
    TableContent,
    TextRun,
    TextStyle,
)
from comet_rag.sources import AsyncLocalSourceResolver, LocalSource
from tests.fixtures.docx.build import (
    build_basic,
    build_equations,
    build_image,
    build_merged_table,
)


class HTML(HTMLParser):
    def __init__(self, value: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.feed(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def document(*blocks: Block, assets: tuple[Asset, ...] = ()) -> ParsedDocument:
    return ParsedDocument(
        SourceInfo("markdown-source", "a" * 64),
        ParsingInfo("test", "1"),
        blocks,
        assets,
    )


def paragraph(block_id: str, value: str, parent: str | None = None) -> Block:
    return Block(
        block_id,
        "paragraph",
        rich_text=RichText((TextRun(value),)),
        parent_block_id=parent,
    )


def parse_docx(path: Path) -> ParsedDocument:
    async def parse() -> ParsedDocument:
        async with AsyncLocalSourceResolver().resolve(LocalSource(path)) as resource:
            result = await AsyncDocxParser().parse(
                resource, output_dir=path.parent / "assets"
            )
            return result.document

    return asyncio.run(parse())


def test_real_text_preserves_unicode_spaces_tabs_and_line_endings(
    tmp_path: Path,
) -> None:
    source = "﻿  α  00123\t12%\r\n\r\n尾行  \n"
    path = tmp_path / "source.txt"
    path.write_bytes(source.encode())

    async def parse() -> ParsedDocument:
        async with AsyncLocalSourceResolver().resolve(LocalSource(path)) as resource:
            return (
                await AsyncTextParser().parse(resource, output_dir=tmp_path / "unused")
            ).document

    result = export_markdown(asyncio.run(parse()))
    assert "".join(HTML(result).text) == source
    assert 'style="white-space: pre-wrap"' in result
    assert not (tmp_path / "unused").exists()


def test_native_docx_heading_lists_and_styles(tmp_path: Path) -> None:
    result = export_markdown(parse_docx(build_basic(tmp_path / "basic.docx")))
    assert result.startswith("# ")
    assert "一级标题" in result
    assert "\n\n## " in result and "\n\n### " in result
    assert "<strong>加粗</strong>" in result
    assert "<em>斜体</em>" in result
    assert "\n\n1. <span " in result
    assert "\n\n2. <span " in result
    assert "有序第一项" in "".join(HTML(result).text)
    assert "有序第二项" in "".join(HTML(result).text)
    assert result.count("第一项") == 2


def test_native_docx_merged_table_children_render_once(tmp_path: Path) -> None:
    result = export_markdown(parse_docx(build_merged_table(tmp_path / "table.docx")))
    parsed = HTML(result)
    assert result.count("<table>") == 2
    assert result.count("合并表头") == 1
    assert result.count("整行合并") == 1
    assert result.count("Q1") == 2
    assert result.count("纵向") == 1
    cells = [attrs for tag, attrs in parsed.tags if tag in {"td", "th"}]
    assert any(cell["rowspan"] == "2" for cell in cells)
    assert any(cell["colspan"] == "3" for cell in cells)
    assert any(cell["colspan"] == "2" for cell in cells)


def test_native_docx_omml_retains_source_and_diagnostic(tmp_path: Path) -> None:
    snapshot = parse_docx(build_equations(tmp_path / "formula.docx"))
    result = export_markdown(snapshot)
    assert "formula source: omml" in result
    assert "<m:oMath" not in result
    assert not any(tag in {"math", "m:omath"} for tag, _ in HTML(result).tags)
    assert "$$" not in result
    formulas = [
        inline.formula.expression
        for block in snapshot.blocks
        if block.rich_text is not None
        for inline in block.rich_text.inlines
        if isinstance(inline, InlineFormula)
    ]
    text = "".join(HTML(result).text)
    assert len(formulas) == 4
    assert all(expression is not None and expression in text for expression in formulas)


def test_native_docx_image_references_materialized_asset(tmp_path: Path) -> None:
    snapshot = parse_docx(build_image(tmp_path / "image.docx"))
    result = export_markdown(snapshot)
    assert result.count("![") == 1
    assert snapshot.assets[0].media.path is not None
    assert str(snapshot.assets[0].media.path) in result
    assert "data:" not in result


def test_text_and_style_values_never_become_tags_or_attributes() -> None:
    source = '  <img src=x onerror="alert(1)">\n[x](javascript:bad) *raw*  '
    style = TextStyle(
        font='font" onmouseover="bad',
        size_pt=11,
        bold=True,
        italic=True,
        underline=True,
        strike=True,
        color="#aabbcc",
        script="superscript",
    )
    result = export_markdown(
        document(Block("p", "paragraph", rich_text=RichText((TextRun(source, style),))))
    )
    parsed = HTML(result)
    assert "".join(parsed.text) == source
    assert {tag for tag, _ in parsed.tags} == {"span", "strong", "em", "u", "s", "sup"}
    span = next(attrs for tag, attrs in parsed.tags if tag == "span")
    assert span["data-font"] == 'font" onmouseover="bad'
    assert set(span) == {"style", "data-font"}
    assert span["style"] == "white-space: pre-wrap; color: #AABBCC; font-size: 11pt"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  a ` b `` c\t\n尾  ",
        "</code><script>bad</script>",
        "[click](javascript:bad)",
    ],
)
def test_inline_code_exact_text_and_order(value: str) -> None:
    result = export_markdown(
        document(
            Block(
                "p",
                "paragraph",
                rich_text=RichText(
                    (TextRun("before"), InlineCode(value), TextRun("after"))
                ),
            )
        )
    )
    parsed = HTML(result)
    assert "".join(parsed.text) == "before" + value + "after"
    assert [tag for tag, _ in parsed.tags] == ["code"]
    assert 'style="white-space: pre-wrap"' in result


def test_code_block_chooses_fence_and_preserves_raw_body() -> None:
    source = "\tprint('a')  \r\n\n```\n````\n<script>source</script>\n"
    result = export_markdown(
        document(Block("code", "code", code=source, language="python", filename="x.py"))
    )
    assert "`````python\n" + source + "`````" in result
    assert "code file:" in result and "x.py" in "".join(HTML(result).text)
    assert "\tprint('a')  \r\n" in result
    assert export_markdown(document(Block("empty", "code", code=""))) == "```\n\n```"


def test_code_language_and_filename_cannot_break_the_fence() -> None:
    result = export_markdown(
        document(
            Block(
                "code",
                "code",
                code="body",
                language="python\n```\n<script>",
                filename="</code><script>bad</script>",
            )
        )
    )
    assert "[code language:" in result
    assert "<script>" not in result
    assert result.endswith("```\nbody\n```")


def test_latex_delimiters_numbers_and_inline_order() -> None:
    result = export_markdown(
        document(
            Block(
                "p",
                "paragraph",
                rich_text=RichText(
                    (
                        TextRun("a"),
                        InlineFormula(FormulaContent(r"x^2 + \alpha", "latex")),
                        TextRun("b"),
                    )
                ),
            ),
            Block(
                "f",
                "formula",
                formula=FormulaContent(r"\frac{1}{2}", "latex"),
                formula_number="(7)",
            ),
        )
    )
    assert result.startswith(r"a$x^2 + \alpha$b")
    assert "$$\n\\frac{1}{2}\n$$" in result
    assert "formula number:" in result and r"\(7\)" in result


@pytest.mark.parametrize(
    ("expression", "kind"),
    [
        ("<math><mi>x</mi></math>", "mathml"),
        ("<m:oMath>x</m:oMath>", "omml"),
        ("a$b", "latex"),
        ("x<y & z", "latex"),
        ("x\n```\ny", "latex"),
    ],
)
def test_non_latex_or_ambiguous_formula_is_readable_source(
    expression: str, kind: str
) -> None:
    content = FormulaContent(expression, kind)  # type: ignore[arg-type]
    block_result = export_markdown(document(Block("f", "formula", formula=content)))
    assert f"[formula source: {kind}]" in block_result
    assert expression in block_result
    assert "$$" not in block_result
    inline_result = export_markdown(
        document(Block("p", "paragraph", rich_text=RichText((InlineFormula(content),))))
    )
    assert expression in "".join(HTML(inline_result).text)
    assert [tag for tag, _ in HTML(inline_result).tags] == ["code"]


def test_cell_headers_unknown_slots_merged_spans_and_nested_order() -> None:
    snapshot = document(
        Block(
            "t",
            "table",
            table=TableContent(
                cells=(
                    TableCell(
                        0, 0, ("p", "code"), row_span=2, column_span=2, header=True
                    ),
                    TableCell(0, 2, (), header=False),
                    TableCell(1, 2, ("nested",)),
                ),
                rows=3,
                columns=3,
            ),
        ),
        paragraph("p", "00123 <script>\n12%", "t"),
        Block("code", "code", code="\tcode  \n", parent_block_id="t"),
        Block(
            "nested",
            "table",
            table=TableContent(cells=(TableCell(0, 0, ("child",)),)),
            parent_block_id="t",
        ),
        paragraph("child", "inside", "nested"),
        paragraph("after", "after"),
    )
    result = export_markdown(snapshot)
    parsed = HTML(result)
    assert result.count("<table>") == 2
    assert result.count("00123") == result.count("inside") == 1
    assert '<th rowspan="2" colspan="2">' in result
    assert '<td rowspan="1" colspan="1"></td>' in result
    assert result.count('data-unknown="true"') == 3
    assert 'data-header="unknown"' in result
    assert "<script>" not in result
    assert "00123 <script>\n12%" in "".join(parsed.text)
    assert "\tcode  \n" in "".join(parsed.text)
    assert result.endswith("</table>\n\nafter")
    assert result.index("00123") < result.index("code") < result.index("inside")


def test_raw_html_table_is_fenced_source_even_with_embedded_fences() -> None:
    source = '<table><script>bad</script><tr><td onclick="bad">```</td></tr></table>'
    result = export_markdown(
        document(Block("t", "table", table=TableContent("html", html=source)))
    )
    assert (
        result
        == "[table HTML source; cell structure unavailable]\n\n````html\n"
        + source
        + "\n````"
    )


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "data:text/html,<script>bad</script>",
        "file:///private/data",
        "vbscript:bad",
        "//external.example/path",
        "\\\\external.example\\path",
        "https://example.org/\npath",
    ],
)
def test_unsafe_links_keep_readable_source_without_active_target(target: str) -> None:
    result = export_markdown(
        document(
            Block(
                "p", "paragraph", rich_text=RichText((TextRun("label", link=target),))
            )
        )
    )
    assert "link omitted:" in result
    assert "href=" not in result and "](<" not in result
    assert target in "".join(HTML(result).text)


@pytest.mark.parametrize(
    ("target", "encoded"),
    [
        ("https://example.org/a b?x=1&y=2", "https://example.org/a%20b?x=1&y=2"),
        ("mailto:user@example.org", "mailto:user@example.org"),
        ("docs/a (b).md#part", "docs/a%20%28b%29.md#part"),
        ("#part", "#part"),
    ],
)
def test_safe_links_encode_destinations(target: str, encoded: str) -> None:
    result = export_markdown(
        document(
            Block(
                "p", "paragraph", rich_text=RichText((TextRun("label", link=target),))
            )
        )
    )
    assert result == f"[label](<{encoded}>)"


def test_images_quote_paths_unknown_types_inline_bytes_and_alt_text() -> None:
    pixels = b"inline source bytes"
    assets = (
        Asset(
            "local", "b" * 64, MediaRef(path='media/a (b)".png', media_type="image/png")
        ),
        Asset(
            "remote",
            "c" * 64,
            MediaRef(url="https://example.org/a b.png", media_type="image/png"),
        ),
        Asset(
            "inline",
            sha256(pixels).hexdigest(),
            MediaRef(data=pixels, media_type="image/png"),
        ),
        Asset(
            "svg", "d" * 64, MediaRef(path="drawing.svg", media_type="image/svg+xml")
        ),
        Asset("unknown", "e" * 64, MediaRef(path="unknown")),
        Asset(
            "colon",
            "f" * 64,
            MediaRef(path="javascript:source.png", media_type="image/png"),
        ),
    )
    snapshot = document(
        *(
            Block(
                asset.asset_id,
                "image",
                asset_id=asset.asset_id,
                alt_text="alt ](<script>)",
            )
            for asset in assets
        ),
        assets=assets,
    )
    result = export_markdown(snapshot)
    assert "media/a%20%28b%29%22.png" in result
    assert "https://example.org/a%20b.png" in result
    assert "javascript%3Asource.png" in result
    assert result.count("image not embedded:") == 3
    assert "inline bytes (19 bytes)" in "".join(HTML(result).text)
    assert sha256(pixels).hexdigest() in result
    assert "drawing.svg" in "".join(HTML(result).text) and "image/svg+xml" in "".join(
        HTML(result).text
    )
    assert "unknown media type" in result
    assert "data:" not in result and "<script>" not in result


def test_image_formula_table_image_and_pages_do_not_duplicate_assets() -> None:
    asset = Asset(
        "pixels", "b" * 64, MediaRef(path="formula.png", media_type="image/png")
    )
    snapshot = document(
        Block("f", "formula", formula=FormulaContent(asset_id="pixels")),
        Block("t", "table", table=TableContent("image", asset_id="pixels")),
        assets=(asset,),
    )
    snapshot = replace(snapshot, pages=(PageInfo("page", asset_id="pixels"),))
    result = export_markdown(snapshot)
    assert result.count("formula.png") == 2
    assert "formula image" in result
    assert "table cell structure unavailable" in result


def test_layout_heading_and_custom_list_markers_keep_source_evidence() -> None:
    result = export_markdown(
        document(
            Block(
                "heading",
                "heading",
                rich_text=RichText((TextRun("title"),)),
                layout={"heading_level": 2},
            ),
            Block("deep", "heading", rich_text=RichText((TextRun("deep"),)), level=9),
            Block(
                "list",
                "list_item",
                rich_text=RichText((TextRun("item"),)),
                level=1,
                marker="iv)",
            ),
            Block(
                "bullet",
                "list_item",
                rich_text=RichText((TextRun("bullet"),)),
                layout={"numbering_format": "bullet", "marker_template": "•"},
            ),
            Block("unknown", "list_item", rich_text=RichText((TextRun("unknown"),))),
        )
    )
    assert result.startswith("## title")
    assert "[heading level 9] deep" in result
    assert '\n\n    - <code style="white-space: pre-wrap">iv&#41;</code> item' in result
    assert "•</code> bullet" in result
    assert "list marker unknown" in result


def test_html_cell_links_images_and_raw_sources_escape_attributes() -> None:
    label = 'label" onerror="bad\n'
    source = "<table><script>bad</script>\n```</table>"
    asset = Asset(
        "pixels", "b" * 64, MediaRef(path='media/a".png', media_type="image/png")
    )
    snapshot = document(
        Block(
            "table",
            "table",
            table=TableContent(
                cells=(TableCell(0, 0, ("linked", "image", "raw", "formula")),)
            ),
        ),
        Block(
            "linked",
            "paragraph",
            rich_text=RichText(
                (TextRun("linked", link='https://example.org/a"?x=1&y=2'),)
            ),
            parent_block_id="table",
        ),
        Block(
            "image", "image", asset_id="pixels", alt_text=label, parent_block_id="table"
        ),
        Block(
            "raw",
            "table",
            table=TableContent("html", html=source),
            parent_block_id="table",
        ),
        Block(
            "formula",
            "formula",
            formula=FormulaContent("x^2", "latex"),
            formula_number="(1)",
            parent_block_id="table",
        ),
        assets=(asset,),
    )
    result = export_markdown(snapshot)
    parsed = HTML(result)
    image = next(attrs for tag, attrs in parsed.tags if tag == "img")
    assert image == {"src": "media/a%22.png", "alt": label}
    link = next(attrs for tag, attrs in parsed.tags if tag == "a")
    assert link == {"href": "https://example.org/a%22?x=1&y=2"}
    assert not any(tag == "script" for tag, _ in parsed.tags)
    assert source in "".join(parsed.text)
    assert "$$\nx^2\n$$" in "".join(parsed.text)
    assert result.count("<table>") == 1


def test_export_recomputes_after_snapshot_and_runtime_asset_replacement() -> None:
    asset = Asset(
        "pixels", "b" * 64, MediaRef(path="before.png", media_type="image/png")
    )
    before = document(
        paragraph("p", "before"),
        Block("i", "image", asset_id="pixels"),
        assets=(asset,),
    )
    first = export_markdown(before)
    after = replace(before, blocks=(paragraph("p", "after"), before.blocks[1]))
    assert before.document_id != after.document_id
    assert export_markdown(after).startswith("after\n\n")
    relocated = replace(
        after,
        assets=(
            replace(asset, media=MediaRef(path="after.png", media_type="image/png")),
        ),
    )
    assert relocated.document_id == after.document_id
    assert "after.png" in export_markdown(relocated)
    assert "before.png" not in export_markdown(relocated)
    assert export_markdown(before) == first
    assert export_markdown(document()) == ""
