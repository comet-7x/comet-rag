"""合成 .docx 测试样本。

**为什么是生成而非提交二进制**：
1. `.docx` 是 zip，进了 git 就是不可 review 的黑盒 —— 谁也说不清某次改动
   到底改了文档的什么
2. 仓库里现有的真实文档（`poc/docs/`）含实际项目内容与内部流程，
   本仓库计划开源，不应把它们提交进来
3. 生成脚本本身就是"样本里有什么"的可读说明

代价是合成文档的 XML 比 Word 真实输出简单，某些 Word 特有的怪异结构
覆盖不到。`test_docx_parser.py` 里另有一条可选用例，本地存在 `poc/docs/`
时会拿真实文档跑冒烟检查，兼顾两头。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from docx import Document
from docx.shared import Inches

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# 1x1 透明 PNG —— 只是为了让 python-docx 有个合法图片可嵌
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _append_omml(paragraph, omml_xml: str) -> None:
    """把一段 OMML 塞进段落。python-docx 没有公式 API，只能直接操作 XML。"""
    from lxml import etree

    fragment = etree.fromstring(f'<m:oMath xmlns:m="{MATH_NS}">{omml_xml}</m:oMath>')
    paragraph._p.append(fragment)


def build_basic(path: Path) -> Path:
    """标题层级 + 段落格式 + 列表。"""
    doc = Document()

    doc.add_heading("一级标题", level=1)
    doc.add_paragraph("这是第一段正文，用于验证纯文本提取。")

    p = doc.add_paragraph()
    p.add_run("加粗").bold = True
    p.add_run("与")
    p.add_run("斜体").italic = True
    p.add_run("混排。")

    doc.add_heading("二级标题", level=2)
    doc.add_paragraph("二级标题下的正文。")

    doc.add_heading("三级标题", level=3)
    doc.add_paragraph("第一项", style="List Bullet")
    doc.add_paragraph("第二项", style="List Bullet")
    doc.add_paragraph("嵌套项", style="List Bullet 2")
    doc.add_paragraph("有序第一项", style="List Number")
    doc.add_paragraph("有序第二项", style="List Number")

    doc.save(str(path))
    return path


def build_table(path: Path) -> Path:
    """表格：含表头、多行、空单元格。"""
    doc = Document()
    doc.add_heading("表格样本", level=1)

    table = doc.add_table(rows=3, cols=3)
    headers = ["列A", "列B", "列C"]
    for i, text in enumerate(headers):
        table.rows[0].cells[i].text = text
    table.rows[1].cells[0].text = "值1"
    table.rows[1].cells[1].text = "值2"
    table.rows[1].cells[2].text = ""  # 空单元格
    table.rows[2].cells[0].text = "值4"
    table.rows[2].cells[1].text = "值5"
    table.rows[2].cells[2].text = "值6"

    doc.add_paragraph("表格后的正文。")
    doc.save(str(path))
    return path


def build_merged_table(path: Path) -> Path:
    """表格：横向合并、纵向合并、**相邻重复值**、相邻空格（#18）。

    每一行都对着一种此前会出错的形态：

        r0  [合并表头 →  ←] [备注]     横向合并（gridSpan=2）
        r1  [Q1] [Q1] [同上]           相邻重复值 —— 旧实现会折叠成 2 列
        r2  [  ] [  ] [尾]             相邻空格 —— 同样会被折叠
        r3  [纵向] [X] [X]             纵向合并（vMerge）+ 又一组重复值
        r4  [ ↑  ] [Y] [Y]
        r5  [整行合并 →   ←   ←]       gridSpan=3，覆盖"续格不止一个"

    第二张表专攻**合并出现在什么位置**（PR #32 评审建议）。上面那张表里的
    合并都在行首，覆盖不到"行尾"与"同一行里有两个分开的合并"：

        t2r0  [首合 ←] [单A] [中合 ←] [单B]    行首合并 + 中段合并（两个分离）
        t2r1  [a] [b] [c] [d] [尾合  ←]        行尾合并

    分成两张表而不是把第一张加宽：加宽会改动那边每一行的宽度与断言，
    而那些断言各自盯着别的东西，不该被这次改动牵连。
    """
    doc = Document()
    doc.add_heading("合并单元格样本", level=1)

    table = doc.add_table(rows=6, cols=3)

    # 先合并再写文本：反过来会把两格的段落拼到一起。
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 0).text = "合并表头"
    table.cell(0, 2).text = "备注"

    for col, text in enumerate(["Q1", "Q1", "同上"]):
        table.cell(1, col).text = text
    for col, text in enumerate(["", "", "尾"]):
        table.cell(2, col).text = text

    table.cell(3, 0).merge(table.cell(4, 0))
    table.cell(3, 0).text = "纵向"
    for col, text in enumerate(["X", "X"], start=1):
        table.cell(3, col).text = text
    for col, text in enumerate(["Y", "Y"], start=1):
        table.cell(4, col).text = text

    # gridSpan=3：续格有两个。按对象同一性判断时这是隐式成立的，
    # 换成按 grid_span 计数后是一处显式算术，值得单独覆盖。
    table.cell(5, 0).merge(table.cell(5, 2))
    table.cell(5, 0).text = "整行合并"

    # 中间必须隔一个段落：OOXML 里两张紧邻的表会被 Word 视作同一张。
    doc.add_paragraph("下面这张表专攻合并出现的位置。")

    positions = doc.add_table(rows=2, cols=6)
    positions.cell(0, 0).merge(positions.cell(0, 1))
    positions.cell(0, 0).text = "首合"
    positions.cell(0, 2).text = "单A"
    positions.cell(0, 3).merge(positions.cell(0, 4))
    positions.cell(0, 3).text = "中合"
    positions.cell(0, 5).text = "单B"

    for col, text in enumerate(["a", "b", "c", "d"]):
        positions.cell(1, col).text = text
    positions.cell(1, 4).merge(positions.cell(1, 5))
    positions.cell(1, 4).text = "尾合"

    doc.save(str(path))
    return path


def build_image(path: Path) -> Path:
    doc = Document()
    doc.add_heading("图片样本", level=1)
    doc.add_paragraph("图片前的说明。")
    doc.add_picture(io.BytesIO(_PNG_1X1), width=Inches(1))
    doc.add_paragraph("图片后的说明。")
    doc.save(str(path))
    return path


def build_equations(path: Path) -> Path:
    """公式：分式、上下标、根号、求和。"""
    doc = Document()
    doc.add_heading("公式样本", level=1)

    def r(t: str) -> str:
        return f"<m:r><m:t>{t}</m:t></m:r>"

    specimens = [
        ("分式", f"<m:f><m:num>{r('a')}</m:num><m:den>{r('b')}</m:den></m:f>"),
        ("上标", f"<m:sSup><m:e>{r('x')}</m:e><m:sup>{r('2')}</m:sup></m:sSup>"),
        ("根号", f"<m:rad><m:deg/><m:e>{r('x')}</m:e></m:rad>"),
        (
            "求和",
            '<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr>'
            f"<m:sub>{r('i=1')}</m:sub><m:sup>{r('n')}</m:sup><m:e>{r('i')}</m:e>"
            "</m:nary>",
        ),
    ]
    for label, omml in specimens:
        p = doc.add_paragraph(f"{label}：")
        _append_omml(p, omml)

    doc.save(str(path))
    return path


def build_header_footer(path: Path) -> Path:
    doc = Document()
    section = doc.sections[0]
    section.header.paragraphs[0].text = "这是页眉"
    section.footer.paragraphs[0].text = "这是页脚"

    doc.add_heading("页眉页脚样本", level=1)
    doc.add_paragraph("正文内容。")
    doc.save(str(path))
    return path


#: 名称 → 构造函数。测试按此表逐个生成并比对快照。
BUILDERS = {
    "basic": build_basic,
    "table": build_table,
    "merged_table": build_merged_table,
    "image": build_image,
    "equations": build_equations,
    "header_footer": build_header_footer,
}


def build_all(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return {name: fn(out_dir / f"{name}.docx") for name, fn in BUILDERS.items()}


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "./_docx_out")
    for name, p in build_all(target).items():
        print(f"{name:16} -> {p}")
