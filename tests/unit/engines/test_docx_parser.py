"""`docx_parser` 快照测试。

962 行、此前零测试，是全仓改动风险最高的文件（plan R2）。本轮**只加保护网、
不重构**：把当前解析结果整体钉住，之后任何改动只要输出变了就会立刻显形。

样本由 `tests/fixtures/docx/build.py` 生成而非提交二进制 —— .docx 是 zip，
进了 git 就是不可 review 的黑盒；而且仓库里现有的真实文档含实际项目内容，
本项目计划开源，不应提交。生成脚本本身就是"样本里有什么"的说明。

**更新快照**（确认差异符合预期后）：

    UPDATE_DOCX_SNAPSHOTS=1 uv run pytest tests/unit/engines/test_docx_parser.py

切勿无脑更新 —— 快照测试的价值全在于"变了要有人看一眼"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from docx import Document

from comet_rag.engines.converters.types import DocxDocument
from comet_rag.engines.parsers.docx_parser.docx_parser import DocxParser
from tests.fixtures.docx.build import BUILDERS, build_all

SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "docx" / "snapshots"
UPDATING = os.environ.get("UPDATE_DOCX_SNAPSHOTS") == "1"


@pytest.fixture(scope="session")
def generated_docx(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return build_all(tmp_path_factory.mktemp("docx"))


def _parse(path: Path, **kwargs) -> dict:
    parsed = DocxParser(**kwargs).parse(
        DocxDocument(elements=Document(str(path)), metadata={})
    )
    return {"blocks": parsed.blocks, "text": parsed.text}


def _assert_matches_snapshot(name: str, actual: dict) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{name}.json"

    if UPDATING or not snapshot_path.exists():
        snapshot_path.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if not UPDATING:
            pytest.fail(
                f"快照 {snapshot_path.name} 不存在，已生成。请人工核对内容后再提交。"
            )
        return

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"{name} 的解析结果与快照不符。确认改动符合预期后，用 "
        f"UPDATE_DOCX_SNAPSHOTS=1 重新生成。"
    )


@pytest.mark.parametrize("name", sorted(BUILDERS), ids=sorted(BUILDERS))
def test_parse_matches_snapshot(name: str, generated_docx: dict[str, Path]) -> None:
    _assert_matches_snapshot(name, _parse(generated_docx[name]))


# ── 针对性断言 ─────────────────────────────────────────────────────────────
#
# 快照能发现"变了"，但说不出"变的是什么、要紧不要紧"。下面这些针对
# 关键语义单独断言，坏掉时报错信息直指问题。


def test_heading_levels_are_preserved(generated_docx: dict[str, Path]) -> None:
    """标题层级丢了，分块就没法按语义边界切，检索质量直接下滑。"""
    blocks = _parse(generated_docx["basic"])["blocks"]
    headings = [(b["level"], b["content"]) for b in blocks if b["type"] == "heading"]

    assert [lvl for lvl, _ in headings] == [1, 2, 3]


def test_inline_formatting_becomes_markdown(generated_docx: dict[str, Path]) -> None:
    blocks = _parse(generated_docx["basic"])["blocks"]
    contents = [b.get("content", "") for b in blocks]

    assert any("**加粗**" in c and "*斜体*" in c for c in contents)


def test_list_items_keep_style_name(generated_docx: dict[str, Path]) -> None:
    """**当前行为**：列表项解析为 text 块，层级只体现在 style 名里
    （`List Bullet` / `List Bullet 2`），没有结构化嵌套。
    `DocxParsedContent.text` 里那条 `list` 分支目前走不到。
    """
    blocks = _parse(generated_docx["basic"])["blocks"]
    styles = [b.get("style") for b in blocks if b["type"] == "text"]

    assert "List Bullet" in styles
    assert "List Bullet 2" in styles
    assert "List Number" in styles
    assert not any(b["type"] == "list" for b in blocks)


def test_table_becomes_markdown_and_keeps_rows(
    generated_docx: dict[str, Path],
) -> None:
    blocks = _parse(generated_docx["table"])["blocks"]
    table = next(b for b in blocks if b["type"] == "table")

    assert table["content"].startswith("| 列A | 列B | 列C |")
    assert "| --- |" in table["content"]
    assert table["rows"][1] == ["值1", "值2", ""], "空单元格必须保留占位"


# ── 合并单元格（#18）──────────────────────────────────────────────────────


def test_repeated_cell_values_are_not_mistaken_for_merges(
    generated_docx: dict[str, Path],
) -> None:
    """**这条就是 #18 的全部理由。**

    旧实现把"相邻文本相等"当成合并单元格的判据，于是一张

        [季度, Q1, Q1]

    的表会被解析成 `[季度, Q1]` —— 整整一列凭空消失，不报错也不打日志，
    一路进到向量库。相邻两列取值相同在真实表格里再常见不过。

    判据必须来自结构（同一个 `<w:tc>`），不能来自数据。
    """
    blocks = _parse(generated_docx["merged_table"])["blocks"]
    rows = next(b for b in blocks if b["type"] == "table")["rows"]

    assert rows[1] == ["Q1", "Q1", "同上"], "相邻的重复值被当成合并副本删掉了"
    assert rows[2] == ["", "", "尾"], "相邻的空单元格被折叠了"


def test_merged_cells_keep_column_alignment(
    generated_docx: dict[str, Path],
) -> None:
    """横向合并的续格**留空**，不能删掉。

    Markdown 没有 colspan，续格只能空着；而删掉会让它后面的列整体左移 ——
    行尾补齐救不回来，"备注"会落到第 1 列去。
    """
    blocks = _parse(generated_docx["merged_table"])["blocks"]
    table = next(b for b in blocks if b["type"] == "table")

    assert table["rows"][0] == ["合并表头", "", "备注"]
    assert table["col_count"] == 3, "col_count 必须是真实的网格宽度"
    assert all(len(r) == 3 for r in table["rows"]), "各行宽度必须一致"


def test_vertical_merge_repeats_down_the_column(
    generated_docx: dict[str, Path],
) -> None:
    """**当前行为**：纵向合并（vMerge）的文本沿列重复出现。

    与横向合并不同，它在每一行里都是独立的 `<w:tc>`，按行处理碰不到它。
    这里显式钉住，是为了让将来真要改成"只在首行出现"时，改动是**有意识**的
    而不是顺手带出来的 —— 对检索来说每行自带上下文其实是好事。
    """
    blocks = _parse(generated_docx["merged_table"])["blocks"]
    rows = next(b for b in blocks if b["type"] == "table")["rows"]

    assert rows[3] == ["纵向", "X", "X"]
    assert rows[4] == ["纵向", "Y", "Y"]


def test_image_block_carries_data_and_format(
    generated_docx: dict[str, Path],
) -> None:
    blocks = _parse(generated_docx["image"])["blocks"]
    image = next(b for b in blocks if b["type"] == "image")

    assert image["format"] == "png"
    assert image["content"], "图片内容为空，视觉模型就没东西可描述"
    assert "alt_text" in image


def test_equations_become_inline_latex(generated_docx: dict[str, Path]) -> None:
    text = _parse(generated_docx["equations"])["text"]

    assert r"$\frac{a}{b}$" in text
    assert "$x^{2}$" in text
    assert r"$\sqrt{x}$" in text
    assert r"$\sum_{i=1}^{n}i$" in text


def test_header_and_footer_are_separate_blocks(
    generated_docx: dict[str, Path],
) -> None:
    """页眉页脚要独立成块，DocxCleaner 才能按配置决定收不收。"""
    blocks = _parse(generated_docx["header_footer"])["blocks"]
    types = [b["type"] for b in blocks]

    assert "header" in types
    assert "footer" in types


def test_heading_numbers_option_changes_output(
    generated_docx: dict[str, Path],
) -> None:
    without = _parse(generated_docx["basic"], heading_numbers=False)
    with_numbers = _parse(generated_docx["basic"], heading_numbers=True)

    assert all(
        b.get("is_numbered") is False
        for b in without["blocks"]
        if b["type"] == "heading"
    )
    # 本样本用的是内置样式、无编号域，故两种设置结果一致；
    # 断言"不崩且形状一致"即可，编号域的样本留给真实文档冒烟。
    assert len(with_numbers["blocks"]) == len(without["blocks"])


# ── 真实文档冒烟（可选）───────────────────────────────────────────────────


REAL_DOCS_DIR = Path(__file__).resolve().parents[3] / "poc" / "docs"


@pytest.mark.skipif(
    not REAL_DOCS_DIR.is_dir() or not list(REAL_DOCS_DIR.glob("*.docx")),
    reason="本地没有 poc/docs/*.docx 真实样本",
)
def test_real_world_documents_parse_without_error() -> None:
    """合成样本的 XML 比 Word 真实输出简单得多，覆盖不到 Word 特有的怪异结构。

    这条用真实文档兜底，但**只做冒烟**（不崩、有产出），不比对内容 ——
    那些文档含实际项目材料，不会进版本库，也就无法维护稳定快照。
    """
    for path in sorted(REAL_DOCS_DIR.glob("*.docx")):
        parsed = DocxParser().parse(
            DocxDocument(elements=Document(str(path)), metadata={})
        )
        assert parsed.blocks, f"{path.name} 解析出 0 个块"
        assert parsed.text.strip(), f"{path.name} 文本为空"
