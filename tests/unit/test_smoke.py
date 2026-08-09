"""冒烟测试：证明测试基建本身是通的。

刻意用真实断言而非 `assert True` —— 一个永远为真的测试给的是虚假的安全感，
比没有测试更糟。这里断言的三条不变式，也是 T12 补齐 chunkers 测试时的基线。
"""

from __future__ import annotations

from comet_rag.engines.chunkers import TextChunker


def test_chunker_splits_long_text_into_multiple_chunks() -> None:
    text = "这是一段用于验证分块器的中文文本。" * 200
    chunks = TextChunker(chunk_size=200, chunk_overlap=20).chunk(text)

    assert len(chunks) > 1, "超过 chunk_size 的文本应被切成多块"
    assert all(c.strip() for c in chunks), "不应产生空白块"


def test_chunker_preserves_content() -> None:
    """分块不得丢内容——这是整条 RAG 链路的底线，丢了就是检索不到。"""
    sentences = [f"第{i}句话的内容各不相同。" for i in range(50)]
    text = "".join(sentences)

    chunks = TextChunker(chunk_size=120, chunk_overlap=0).chunk(text)

    joined = "".join(chunks)
    for sentence in sentences:
        assert sentence in joined, f"内容丢失：{sentence!r}"


def test_chunker_handles_empty_text() -> None:
    assert TextChunker().chunk("") == []
