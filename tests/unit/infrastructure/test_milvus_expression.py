"""`build_expression` 的对抗性输入（PR 评审 #5）。

这个函数是**唯一**把结构化 dict 翻译成 Milvus 表达式的地方，也就是唯一
"字符串拼接出查询语句"的地方 —— 注入类问题只可能出在这里。

filter 来自 HTTP 请求体（`POST /search` 的 `filter` 字段），**键和值都是
调用方完全可控的**。最初只转义了值，键是直接插进 `metadata["..."]` 的。

不需要 Milvus 也能跑：翻译是纯函数。
"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.vectorstore.milvus import build_expression

#: 能改变表达式结构的字符
HOSTILE = [
    '"] or metadata["x',  # 闭合引号后接一个新谓词
    'a"b',  # 裸引号
    "a\\b",  # 反斜杠（转义符本身）
    'a\\"b',  # 已转义的引号，别转义两次转成别的东西
    "a]b[c",  # 方括号
]


@pytest.mark.parametrize("key", HOSTILE, ids=range(len(HOSTILE)))
def test_hostile_keys_stay_inside_one_quoted_path(key: str) -> None:
    """恶意键不得逃出它那对引号。

    判据用引号计数而不是"长得对不对"：合法表达式里引号必须成对，
    而注入的本质就是**多出或少掉一个引号**。
    """
    expr = build_expression({key: "v"})

    unescaped_quotes = _count_unescaped(expr, '"')
    # metadata["<key>"] == "<value>" → 恰好 4 个未转义引号
    assert unescaped_quotes == 4, (
        f"未转义引号有 {unescaped_quotes} 个（应为 4），表达式结构被键改写了：{expr}"
    )
    # 注意别断言 `" or " not in expr`：转义之后那几个字符**仍然在**表达式里，
    # 只是老老实实待在引号内当普通文本。安全性质是"引号没被逃出去"，
    # 不是"敏感词没出现" —— 后者是一条看着很像但其实测错了东西的断言。


@pytest.mark.parametrize("key", HOSTILE, ids=range(len(HOSTILE)))
def test_hostile_keys_do_not_add_predicates(key: str) -> None:
    """一个键值对只能产生一个谓词。"""
    assert build_expression({key: "v"}).count("==") == 1


def test_hostile_values_are_still_escaped() -> None:
    """值的转义是原本就有的，别在改键的时候把它弄丢了。"""
    expr = build_expression({"k": '"] or metadata["x'})
    assert _count_unescaped(expr, '"') == 4, expr


def test_empty_or_non_string_key_is_rejected() -> None:
    """空键在 Milvus 里没有意义，早点报错比生成一条查不出东西的表达式好。"""
    with pytest.raises(ValueError, match="非空字符串"):
        build_expression({"": 1})
    with pytest.raises(ValueError, match="非空字符串"):
        build_expression({1: 1})  # type: ignore[dict-item]


def test_normal_keys_are_unchanged() -> None:
    """别为了防注入把正常用法也改了。"""
    assert build_expression({"kb_id": "demo"}) == 'metadata["kb_id"] == "demo"'
    assert build_expression({"n": 3}) == 'metadata["n"] == 3'
    assert build_expression({"t": ["a", "b"]}) == 'metadata["t"] in ["a", "b"]'
    assert build_expression(None) == ""
    assert build_expression({}) == ""


def _count_unescaped(text: str, char: str) -> int:
    """数没有被反斜杠转义的 `char`。"""
    count = i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2  # 跳过被转义的那个字符
            continue
        if text[i] == char:
            count += 1
        i += 1
    return count
