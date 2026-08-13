"""分块器：不变式与边界。

分块是 RAG 链路里最容易"看起来对"的一环 —— 切错了不会报错，
只会让检索悄悄变差。所以这里以**不变式**为主：不去断言具体切在哪，
而是断言无论怎么切都必须成立的性质。

下面三条不变式是先跑实验确认过的，不是假设：
  1. 每个 chunk 长度 ≤ chunk_size
  2. chunk_overlap=0 时，拼接结果与原文**逐字相等**（不丢不改不重排）
  3. 空白输入产出空列表，而不是 [""] 之类的脏数据

`chunk_overlap >= chunk_size` 不是边界值而是非法配置，构造时即 ValueError。
"""

from __future__ import annotations

import pytest

from comet_rag.engines.chunkers import (
    CsvChunker,
    DocxChunker,
    JsonChunker,
    MdxChunker,
    PythonChunker,
    TextChunker,
    TypeScriptChunker,
    XmlChunker,
)

ALL_CHUNKERS = [
    TextChunker,
    DocxChunker,
    MdxChunker,
    PythonChunker,
    TypeScriptChunker,
    CsvChunker,
    JsonChunker,
    XmlChunker,
]

pytestmark = pytest.mark.parametrize(
    "chunker_cls", ALL_CHUNKERS, ids=[c.__name__ for c in ALL_CHUNKERS]
)

# 必须带真实分隔符结构（段落、换行、句末、空格）。
#
# 教训：最初这里是一串无空格无换行的中文，结果所有"不变式"测试都从
# 分隔符递归逻辑旁边绕了过去，直接落到"按字符强制切分"的退化分支 ——
# 覆盖率报告显示 _split_text_with_separator 整段未执行才发现。
# 测试通过了，但测的不是真正会跑的那条路。
PROSE = "\n\n".join(
    " ".join(f"Paragraph {p} sentence {s} with several words." for s in range(6))
    for p in range(12)
)


# ── 不变式 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("chunk_size", [40, 120, 400])
def test_no_chunk_exceeds_chunk_size(chunker_cls, chunk_size: int) -> None:
    chunks = chunker_cls(chunk_size=chunk_size, chunk_overlap=0).chunk(PROSE)

    oversized = [(i, len(c)) for i, c in enumerate(chunks) if len(c) > chunk_size]
    assert not oversized, f"以下 chunk 超过 {chunk_size}：{oversized[:5]}"


def test_zero_overlap_reconstructs_text_exactly(chunker_cls) -> None:
    """不丢内容是整条 RAG 链路的底线 —— 丢了就是永远检索不到。"""
    chunks = chunker_cls(chunk_size=120, chunk_overlap=0).chunk(PROSE)

    assert "".join(chunks) == PROSE


def test_no_empty_chunks(chunker_cls) -> None:
    """空 chunk 会白白占一条向量，还会污染检索结果。"""
    chunks = chunker_cls(chunk_size=60, chunk_overlap=10).chunk(PROSE)

    assert all(c for c in chunks)


def test_chunks_are_ordered_substrings(chunker_cls) -> None:
    """顺序必须保持 —— chunk_index 是拼回上下文的唯一依据。"""
    chunks = chunker_cls(chunk_size=120, chunk_overlap=0).chunk(PROSE)

    cursor = 0
    for chunk in chunks:
        found = PROSE.find(chunk, cursor)
        assert found >= 0, f"chunk 不是原文子串：{chunk[:40]!r}"
        cursor = found


def test_overlap_materializes_when_splits_are_finer_than_overlap(
    chunker_cls,
) -> None:
    """重叠按**整个 split** 保留，不是按字符切。

    因此只有当单个 split 小于 chunk_overlap 时，重叠才真正产生。
    这里用逐字符粒度的文本（无任何分隔符）确保 split 足够细。
    """
    blob = "A" * 600

    no_overlap = "".join(chunker_cls(chunk_size=100, chunk_overlap=0).chunk(blob))
    with_overlap = "".join(chunker_cls(chunk_size=100, chunk_overlap=30).chunk(blob))

    assert len(with_overlap) > len(no_overlap)


# ── 边界 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t \n"])
def test_blank_input_yields_nothing(chunker_cls, text: str) -> None:
    assert chunker_cls(chunk_size=100, chunk_overlap=10).chunk(text) == []


def test_single_character(chunker_cls) -> None:
    assert chunker_cls(chunk_size=100, chunk_overlap=10).chunk("x") == ["x"]


def test_text_shorter_than_chunk_size_stays_whole(chunker_cls) -> None:
    text = "短文本，不该被切开。"

    assert chunker_cls(chunk_size=1000, chunk_overlap=100).chunk(text) == [text]


def test_long_text_without_separators_is_force_split(chunker_cls) -> None:
    """无任何分隔符时必须按字符兜底，不能返回一个超长 chunk。"""
    blob = "A" * 500

    chunks = chunker_cls(chunk_size=100, chunk_overlap=0).chunk(blob)

    assert len(chunks) == 5
    assert all(len(c) == 100 for c in chunks)
    assert "".join(chunks) == blob


def test_overlap_not_smaller_than_size_is_rejected(chunker_cls) -> None:
    """非法配置在构造时就报错，而不是切出诡异结果让人事后猜。"""
    with pytest.raises(ValueError, match="重叠字符数"):
        chunker_cls(chunk_size=50, chunk_overlap=50)

    with pytest.raises(ValueError, match="重叠字符数"):
        chunker_cls(chunk_size=50, chunk_overlap=100)


def test_chunk_size_of_one(chunker_cls) -> None:
    chunks = chunker_cls(chunk_size=1, chunk_overlap=0).chunk("abcde")

    assert chunks == ["a", "b", "c", "d", "e"]


# ── 默认值 ─────────────────────────────────────────────────────────────────
#
# 默认值写在 docs/pipeline_usage.md 的对照表里，改了要同步改文档。


DOCUMENTED_DEFAULTS = {
    TextChunker: (1500, 150),
    DocxChunker: (2500, 250),
    MdxChunker: (3000, 300),
    PythonChunker: (1500, 150),
    TypeScriptChunker: (1500, 150),
    CsvChunker: (1200, 100),
    JsonChunker: (2000, 200),
    XmlChunker: (2500, 250),
}


def test_documented_defaults(chunker_cls) -> None:
    size, _overlap = DOCUMENTED_DEFAULTS[chunker_cls]
    text = "x" * (size * 2)

    chunks = chunker_cls().chunk(text)

    assert all(len(c) <= size for c in chunks)
    # 有 overlap 时总长必大于原文，借此反推默认 overlap 确实非零
    assert len("".join(chunks)) > len(text)


def test_defaults_table_covers_every_chunker(chunker_cls) -> None:
    """新增 chunker 时提醒把默认值补进表里（也补进 docs 的对照表）。"""
    assert chunker_cls in DOCUMENTED_DEFAULTS
