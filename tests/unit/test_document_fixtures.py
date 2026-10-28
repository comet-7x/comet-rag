"""校验生成的源文件内容，不约束产品解析器或规范化文档 schema。"""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree as ET

from tests.fixtures.docx.build import (
    build_basic,
    build_equations,
    build_grid_gaps_table,
    build_header_footer,
    build_image,
    build_merged_table,
    build_table,
)
from tests.fixtures.pdf import (
    build_minimal_pdf,
    build_scanned_pdf,
    minimal_pdf_bytes,
    scanned_pdf_bytes,
)

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rels": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_W_VAL = f"{{{_NS['w']}}}val"


def _part(path: Path, member: str = "word/document.xml") -> ET._Element:
    with ZipFile(path) as archive:
        return ET.fromstring(
            archive.read(member),
            parser=ET.XMLParser(resolve_entities=False, no_network=True),
        )


def _text(element: ET._Element) -> str:
    return "".join(node.text or "" for node in element.findall(".//w:t", _NS))


def test_basic_docx_contains_headings_runs_and_lists(tmp_path: Path) -> None:
    root = _part(build_basic(tmp_path / "basic.docx"))
    paragraphs = root.findall("w:body/w:p", _NS)
    assert [_text(p) for p in paragraphs] == [
        "一级标题",
        "这是第一段正文，用于验证纯文本提取。",
        "加粗与斜体混排。",
        "二级标题",
        "二级标题下的正文。",
        "三级标题",
        "第一项",
        "第二项",
        "嵌套项",
        "有序第一项",
        "有序第二项",
    ]
    assert [
        style.get(_W_VAL) for style in root.findall("w:body/w:p/w:pPr/w:pStyle", _NS)
    ] == [
        "Heading1",
        "Heading2",
        "Heading3",
        "ListBullet",
        "ListBullet",
        "ListBullet2",
        "ListNumber",
        "ListNumber",
    ]
    runs = paragraphs[2].findall("w:r", _NS)
    assert [_text(run) for run in runs] == ["加粗", "与", "斜体", "混排。"]
    assert runs[0].find("w:rPr/w:b", _NS) is not None
    assert runs[2].find("w:rPr/w:i", _NS) is not None


def test_table_docx_keeps_empty_source_cells_and_following_text(tmp_path: Path) -> None:
    root = _part(build_table(tmp_path / "table.docx"))
    rows = root.findall("w:body/w:tbl/w:tr", _NS)
    assert [[_text(cell) for cell in row.findall("w:tc", _NS)] for row in rows] == [
        ["列A", "列B", "列C"],
        ["值1", "值2", ""],
        ["值4", "值5", "值6"],
    ]
    assert _text(root.findall("w:body/w:p", _NS)[-1]) == "表格后的正文。"


def test_merged_docx_tables_keep_raw_spans_and_repeated_cells(tmp_path: Path) -> None:
    root = _part(build_merged_table(tmp_path / "merged.docx"))
    table, positions = root.findall("w:body/w:tbl", _NS)
    assert [
        [_text(cell) for cell in row.findall("w:tc", _NS)]
        for row in table.findall("w:tr", _NS)
    ] == [
        ["合并表头", "备注"],
        ["Q1", "Q1", "同上"],
        ["", "", "尾"],
        ["纵向", "X", "X"],
        ["", "Y", "Y"],
        ["整行合并"],
    ]
    assert [span.get(_W_VAL) for span in table.findall(".//w:gridSpan", _NS)] == [
        "2",
        "3",
    ]
    assert [
        merge.get(_W_VAL, "continue") for merge in table.findall(".//w:vMerge", _NS)
    ] == ["restart", "continue"]
    assert [
        [_text(cell) for cell in row.findall("w:tc", _NS)]
        for row in positions.findall("w:tr", _NS)
    ] == [["首合", "单A", "中合", "单B"], ["a", "b", "c", "d", "尾合"]]
    for cell_path in ("w:tr[1]/w:tc[1]", "w:tr[1]/w:tc[3]", "w:tr[2]/w:tc[5]"):
        assert [
            span.get(_W_VAL)
            for span in positions.findall(f"{cell_path}/w:tcPr/w:gridSpan", _NS)
        ] == ["2"]


def test_grid_gaps_docx_distinguishes_missing_columns_from_empty_cells(
    tmp_path: Path,
) -> None:
    root = _part(build_grid_gaps_table(tmp_path / "gaps.docx"))
    table, combined = root.findall("w:body/w:tbl", _NS)
    rows = table.findall("w:tr", _NS)
    assert [[_text(cell) for cell in row.findall("w:tc", _NS)] for row in rows] == [
        ["A", "B", "C"],
        ["y", "z"],
        ["p", "q"],
        ["m"],
    ]
    assert [
        [node.get(_W_VAL) for node in row.findall("w:trPr/w:gridBefore", _NS)]
        for row in rows
    ] == [[], ["1"], [], ["1"]]
    assert [
        [node.get(_W_VAL) for node in row.findall("w:trPr/w:gridAfter", _NS)]
        for row in rows
    ] == [[], [], ["1"], ["1"]]
    row = combined.findall("w:tr", _NS)[1]
    assert [_text(cell) for cell in row.findall("w:tc", _NS)] == ["合并", "末"]
    assert [node.get(_W_VAL) for node in row.findall("w:trPr/w:gridBefore", _NS)] == [
        "1"
    ]
    assert [
        node.get(_W_VAL) for node in row.findall("w:tc[1]/w:tcPr/w:gridSpan", _NS)
    ] == ["2"]


def test_image_docx_embeds_a_png_between_source_paragraphs(tmp_path: Path) -> None:
    path = build_image(tmp_path / "image.docx")
    root = _part(path)
    assert [_text(p) for p in root.findall("w:body/w:p", _NS)] == [
        "图片样本",
        "图片前的说明。",
        "",
        "图片后的说明。",
    ]
    blip = root.find(".//a:blip", _NS)
    assert blip is not None
    relation_id = blip.attrib[f"{{{_NS['r']}}}embed"]
    relation = _part(path, "word/_rels/document.xml.rels").find(
        f"rels:Relationship[@Id='{relation_id}']", _NS
    )
    assert relation is not None
    assert relation.attrib["Type"] == f"{_NS['r']}/image"
    with ZipFile(path) as archive:
        png = archive.read(f"word/{relation.attrib['Target']}")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert (int.from_bytes(png[16:20]), int.from_bytes(png[20:24])) == (1, 1)


def test_equations_docx_keeps_native_omml_content(tmp_path: Path) -> None:
    root = _part(build_equations(tmp_path / "equations.docx"))
    fraction, power, radical, summation = root.findall(".//m:oMath", _NS)
    assert fraction.findtext("m:f/m:num/m:r/m:t", namespaces=_NS) == "a"
    assert fraction.findtext("m:f/m:den/m:r/m:t", namespaces=_NS) == "b"
    assert power.findtext("m:sSup/m:e/m:r/m:t", namespaces=_NS) == "x"
    assert power.findtext("m:sSup/m:sup/m:r/m:t", namespaces=_NS) == "2"
    assert radical.find("m:rad/m:deg", _NS) is not None
    assert radical.findtext("m:rad/m:e/m:r/m:t", namespaces=_NS) == "x"
    operator = summation.find("m:nary/m:naryPr/m:chr", _NS)
    assert operator is not None
    assert operator.get(f"{{{_NS['m']}}}val") == "∑"
    assert summation.findtext("m:nary/m:sub/m:r/m:t", namespaces=_NS) == "i=1"
    assert summation.findtext("m:nary/m:sup/m:r/m:t", namespaces=_NS) == "n"
    assert summation.findtext("m:nary/m:e/m:r/m:t", namespaces=_NS) == "i"


def test_header_footer_docx_keeps_related_parts_separate_from_body(
    tmp_path: Path,
) -> None:
    path = build_header_footer(tmp_path / "header_footer.docx")
    root = _part(path)
    assert [_text(p) for p in root.findall("w:body/w:p", _NS)] == [
        "页眉页脚样本",
        "正文内容。",
    ]
    relations = _part(path, "word/_rels/document.xml.rels")
    for kind, text in (("header", "这是页眉"), ("footer", "这是页脚")):
        reference = root.find(f"w:body/w:sectPr/w:{kind}Reference", _NS)
        assert reference is not None
        relation_id = reference.attrib[f"{{{_NS['r']}}}id"]
        relation = relations.find(f"rels:Relationship[@Id='{relation_id}']", _NS)
        assert relation is not None
        assert relation.attrib["Type"] == f"{_NS['r']}/{kind}"
        assert _text(_part(path, f"word/{relation.attrib['Target']}")) == text


@pytest.mark.parametrize(
    ("builder", "contents"),
    [(build_minimal_pdf, minimal_pdf_bytes), (build_scanned_pdf, scanned_pdf_bytes)],
    ids=["text", "scanned"],
)
def test_pdf_builders_write_complete_files_with_valid_xrefs(
    tmp_path: Path,
    builder: Callable[[Path], Path],
    contents: Callable[[], bytes],
) -> None:
    path = tmp_path / "sample.pdf"
    assert builder(path) == path
    pdf = path.read_bytes()
    assert pdf == contents()
    assert pdf.startswith(b"%PDF-1.4\n")
    assert pdf.endswith(b"%%EOF\n")
    assert b"/Type /Pages /Kids [3 0 R] /Count 1" in pdf
    xref_offset = int(pdf.rsplit(b"startxref\n", 1)[1].splitlines()[0])
    xref = pdf[xref_offset:].splitlines()
    assert xref[:3] == [b"xref", b"0 6", b"0000000000 65535 f "]
    for number, entry in enumerate(xref[3:8], start=1):
        offset, generation, status = entry.split()
        assert (generation, status) == (b"00000", b"n")
        assert pdf[int(offset) :].startswith(f"{number} 0 obj\n".encode())
    assert xref[8:10] == [b"trailer", b"<< /Root 1 0 R /Size 6 >>"]


def test_text_pdf_contains_a_text_layer_and_font(tmp_path: Path) -> None:
    pdf = build_minimal_pdf(tmp_path / "text.pdf").read_bytes()
    assert b"BT /F1 24 Tf 72 700 Td (Hello PDF) Tj ET" in pdf
    assert b"/Font << /F1 5 0 R >>" in pdf
    assert b"/Subtype /Type1 /BaseFont /Helvetica" in pdf
    assert b"/Subtype /Image" not in pdf


def test_scanned_pdf_contains_a_compressed_image_without_a_text_layer(
    tmp_path: Path,
) -> None:
    pdf = build_scanned_pdf(tmp_path / "scanned.pdf").read_bytes()
    assert b"/XObject << /Im0 5 0 R >>" in pdf
    assert b"q 600 0 0 88 18 360 cm /Im0 Do Q" in pdf
    assert b"/Font" not in pdf
    assert b"BT " not in pdf
    image = re.search(
        rb"<< /Length (\d+) /Type /XObject /Subtype /Image "
        rb"/Width (\d+) /Height (\d+) /ColorSpace /DeviceGray "
        rb"/BitsPerComponent 8 /Filter /FlateDecode >>\nstream\n",
        pdf,
    )
    assert image is not None
    length, width, height = map(int, image.groups())
    assert (width, height) == (600, 88)
    pixels = zlib.decompress(pdf[image.end() : image.end() + length])
    assert len(pixels) == width * height
    assert set(pixels) == {0, 255}
    assert pixels[: width * 16] == b"\xff" * (width * 16)
    assert pixels[width * 16 + 16 : width * 16 + 56] == b"\xff" * 8 + b"\0" * 32
