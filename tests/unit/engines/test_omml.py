"""OMML → LaTeX 转换（`docx_parser/omml.py`，616 行独立逻辑）。

这个模块不依赖 python-docx，只吃 lxml element，所以可以脱离 .docx 直接测 ——
输入是 XML 字符串，输出是 LaTeX 字符串，边界清晰。

公式是 docx 解析里最容易静默出错的部分：转错了不会抛异常，只会得到一段
看起来还行、实际语义已变的 LaTeX，一路灌进向量库。
"""

from __future__ import annotations

import pytest
from lxml import etree

from comet_rag.engines.parsers.docx_parser.omml import escape_latex, oMath2Latex

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def to_latex(inner_xml: str) -> str:
    root = etree.fromstring(f'<m:oMath xmlns:m="{MATH_NS}">{inner_xml}</m:oMath>')
    return str(oMath2Latex(root))


def run(text: str) -> str:
    """一个 `<m:r>` 文本 run —— OMML 里的叶子节点。"""
    return f"<m:r><m:t>{text}</m:t></m:r>"


# ── 基本构造 ───────────────────────────────────────────────────────────────


def test_plain_run() -> None:
    assert to_latex(run("x+y")) == "x+y"


def test_fraction() -> None:
    assert (
        to_latex(f"<m:f><m:num>{run('a')}</m:num><m:den>{run('b')}</m:den></m:f>")
        == r"\frac{a}{b}"
    )


def test_superscript() -> None:
    assert (
        to_latex(f"<m:sSup><m:e>{run('x')}</m:e><m:sup>{run('2')}</m:sup></m:sSup>")
        == "x^{2}"
    )


def test_subscript() -> None:
    assert (
        to_latex(f"<m:sSub><m:e>{run('a')}</m:e><m:sub>{run('n')}</m:sub></m:sSub>")
        == "a_{n}"
    )


def test_sub_and_superscript_order() -> None:
    """下标必须在上标之前 —— `x^{2}_{i}` 与 `x_{i}^{2}` 在某些渲染器下不等价。"""
    latex = to_latex(
        f"<m:sSubSup><m:e>{run('x')}</m:e>"
        f"<m:sub>{run('i')}</m:sub><m:sup>{run('2')}</m:sup></m:sSubSup>"
    )

    assert latex == "x_{i}^{2}"


def test_square_root() -> None:
    assert to_latex(f"<m:rad><m:deg/><m:e>{run('x')}</m:e></m:rad>") == r"\sqrt{x}"


def test_nth_root() -> None:
    assert (
        to_latex(f"<m:rad><m:deg>{run('3')}</m:deg><m:e>{run('x')}</m:e></m:rad>")
        == r"\sqrt[3]{x}"
    )


def test_summation_with_bounds() -> None:
    latex = to_latex(
        '<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr>'
        f"<m:sub>{run('i=1')}</m:sub><m:sup>{run('n')}</m:sup><m:e>{run('i')}</m:e>"
        "</m:nary>"
    )

    assert latex == r"\sum_{i=1}^{n}i"


def test_named_function() -> None:
    latex = to_latex(
        f"<m:func><m:fName>{run('sin')}</m:fName><m:e>{run('x')}</m:e></m:func>"
    )

    assert latex == r"\sin(x)"


def test_overline() -> None:
    latex = to_latex(
        f'<m:bar><m:barPr><m:pos m:val="top"/></m:barPr><m:e>{run("x")}</m:e></m:bar>'
    )

    assert latex == r"\overline{x}"


# ── 定界符 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("beg", "end", "expected"),
    [
        ("(", ")", r"\left(x\right)"),
        ("[", "]", r"\left[x\right]"),
        ("{", "}", r"\left\{x\right\}"),
    ],
)
def test_explicit_delimiters(beg: str, end: str, expected: str) -> None:
    latex = to_latex(
        f'<m:d><m:dPr><m:begChr m:val="{beg}"/><m:endChr m:val="{end}"/></m:dPr>'
        f"<m:e>{run('x')}</m:e></m:d>"
    )

    assert latex == expected


def test_delimiter_without_chars_uses_defaults() -> None:
    """`dPr` 在但没写 begChr/endChr 时，按 OOXML 默认补圆括号。"""
    latex = to_latex(f"<m:d><m:dPr/><m:e>{run('x')}</m:e></m:d>")

    assert latex == r"\left(x\right)"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "疑似缺陷：`<m:d>` 完全没有 dPr 时，do_d(omml.py:360) 直接返回裸内容，"
        "括号整个丢失。而 latex_dict.D_DEFAULT 已经定义了 left='(' right=')'，"
        "同模块的 do_f 在 fPr 缺失时也会套用默认值 —— 说明这里是漏了。"
        "后果：Word 里输入的 (a+b) 可能解析成 a+b，分组语义丢失且不报错。"
        "M1 内不重构 docx_parser（见 plan R2），故先标记。"
    ),
)
def test_delimiter_without_dpr_should_still_add_parentheses() -> None:
    latex = to_latex(f"<m:d><m:e>{run('x+1')}</m:e></m:d>")

    assert latex == r"\left(x+1\right)"


def test_delimiter_without_dpr_current_behaviour() -> None:
    """把当前行为钉住：修好之后这条会失败，提醒同步更新上面的 xfail。"""
    assert to_latex(f"<m:d><m:e>{run('x+1')}</m:e></m:d>") == "x+1"


# ── 转义 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100%", r"100\%"),
        ("a_b", r"a\_b"),
        ("x&y", r"x\&y"),
        ("#1", r"\#1"),
        ("{a}", r"\{a\}"),
        ("no-special", "no-special"),
    ],
)
def test_escape_latex(raw: str, expected: str) -> None:
    """未转义的 % 会把该行之后的 LaTeX 全部注释掉 —— 静默吞内容。"""
    assert escape_latex(raw) == expected


# ── 组合与健壮性 ───────────────────────────────────────────────────────────


def test_nested_fraction_in_superscript() -> None:
    inner = f"<m:f><m:num>{run('1')}</m:num><m:den>{run('2')}</m:den></m:f>"
    latex = to_latex(f"<m:sSup><m:e>{run('e')}</m:e><m:sup>{inner}</m:sup></m:sSup>")

    assert latex == r"e^{\frac{1}{2}}"


def test_multiple_siblings_are_concatenated() -> None:
    latex = to_latex(
        run("a+")
        + f"<m:f><m:num>{run('b')}</m:num><m:den>{run('c')}</m:den></m:f>"
        + run("+d")
    )

    assert latex == r"a+\frac{b}{c}+d"


def test_empty_math_yields_empty_string() -> None:
    assert to_latex("") == ""


def test_unknown_tag_is_skipped_not_fatal() -> None:
    """未知构造不该让整篇文档解析失败 —— 宁可少一段公式。"""
    latex = to_latex(f"<m:completelyUnknownTag>{run('x')}</m:completelyUnknownTag>")

    assert isinstance(latex, str)
