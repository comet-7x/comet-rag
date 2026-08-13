"""分块器的其余语言与配置分支。

与 `test_chunkers.py` 分开：那里是对 8 个主力 chunker 的统一参数化不变式，
这里是不适合塞进那套参数化的单点分支（其余代码语言、CJK 分隔符、
keep_separator=False、MdxChunker 的首行标题特例）。
"""

from __future__ import annotations

import pytest

from comet_rag.engines.chunkers import (
    CChunker,
    CppChunker,
    GoChunker,
    HtmlChunker,
    JavaChunker,
    JavaScriptChunker,
    Language,
    MdxChunker,
    PhpChunker,
    RChunker,
    RustChunker,
    TextChunker,
)

OTHER_CODE_CHUNKERS = [
    CChunker,
    CppChunker,
    GoChunker,
    HtmlChunker,
    JavaChunker,
    JavaScriptChunker,
    PhpChunker,
    RChunker,
    RustChunker,
]


@pytest.mark.parametrize(
    "cls", OTHER_CODE_CHUNKERS, ids=[c.__name__ for c in OTHER_CODE_CHUNKERS]
)
def test_all_code_chunkers_split_without_losing_content(cls) -> None:
    """11 个代码分块器都要能跑通，不只是被主参数化覆盖的那两个。"""
    source = "\n".join(f"line_{i} = compute(value_{i})" for i in range(60))

    chunks = cls(chunk_size=120, chunk_overlap=0).chunk(source)

    assert chunks
    assert "".join(chunks) == source
    assert all(len(c) <= 120 for c in chunks)


def test_mdx_chunker_handles_leading_heading() -> None:
    r"""Markdown 分隔符以 \n 开头，文档首行的 # 标题匹配不上，
    故 MdxChunker 会预置一个 \n —— 但不得因此丢失或污染内容。"""
    text = "# 标题\n\n正文段落。\n\n## 二级标题\n\n更多正文。"

    chunks = MdxChunker(chunk_size=1000, chunk_overlap=0).chunk(text)

    assert "".join(chunks).lstrip("\n") == text
    assert chunks[0].lstrip("\n").startswith("#")


def test_mdx_chunker_without_leading_heading_is_untouched() -> None:
    text = "正文先行。\n\n# 后面才有标题\n\n收尾。"

    chunks = MdxChunker(chunk_size=1000, chunk_overlap=0).chunk(text)

    assert "".join(chunks) == text


@pytest.mark.parametrize(
    "language", [Language.CHINESE, Language.JAPANESE, Language.KOREAN]
)
def test_cjk_separators_preserve_content(language: Language) -> None:
    """中日韩标点与英文不同，用错分隔符会切在句子中间。"""
    text = "".join(f"这是第{i}个句子。" for i in range(40))

    chunks = TextChunker(chunk_size=60, chunk_overlap=0, language=language).chunk(text)

    assert "".join(chunks) == text
    assert all(len(c) <= 60 for c in chunks)


def test_keep_separator_false_still_preserves_length_bound() -> None:
    """keep_separator=False 时分隔符从片段里剥离、合并时重新插回，
    这条路径的长度计算与 True 时不同，容易在边界上溢出。"""
    text = "\n\n".join(f"段落{i}的内容。" for i in range(10))

    chunks = TextChunker(chunk_size=60, chunk_overlap=0, keep_separator=False).chunk(
        text
    )

    assert chunks
    assert all(len(c) <= 60 for c in chunks)


# ── 已知局限 ───────────────────────────────────────────────────────────────


def test_overlap_silently_does_nothing_when_splits_are_coarse() -> None:
    """**已知局限，非缺陷**：单个 split 大于 chunk_overlap 时重叠不生效。

    `_merge_splits` 收缩窗口是整段 `popleft` 的，不按字符切。TextChunker
    在英文散文上以句子为 split（约 45 字符），遇上 overlap=5 会被整个丢弃，
    什么也留不下 —— 输出与 overlap=0 完全一致，且没有任何报错。

    对 RAG 的实际影响：你设 overlap 是为了防止语义在 chunk 边界被切断，
    但在句子粒度的文本上它可能压根没起作用。真正的字符级重叠需要改
    `_merge_splits` 的收缩策略（M1 之外）。

    是否触发取决于**分隔符集与文本的匹配程度**，而非 chunker 类型 ——
    同样的文本给代码分块器（分隔符匹配不上、落到更细粒度）反而会产生重叠。

    本用例把当前行为钉住：哪天它变了（无论修好还是改坏）都会立刻被发现。
    """
    sentences = " ".join(
        f"Sentence number {i} padded with filler words." for i in range(40)
    )

    no_overlap = TextChunker(chunk_size=100, chunk_overlap=0).chunk(sentences)
    with_overlap = TextChunker(chunk_size=100, chunk_overlap=5).chunk(sentences)

    assert with_overlap == no_overlap


def test_overlap_does_materialize_for_fine_grained_splits() -> None:
    """对照组：split 足够细时重叠正常工作 —— 证明上面不是"重叠功能坏了"。"""
    blob = "A" * 600

    no_overlap = TextChunker(chunk_size=100, chunk_overlap=0).chunk(blob)
    with_overlap = TextChunker(chunk_size=100, chunk_overlap=30).chunk(blob)

    assert len("".join(with_overlap)) > len("".join(no_overlap))
