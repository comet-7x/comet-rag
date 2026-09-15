"""Markdown 标题结构分析不变式。"""

from __future__ import annotations

import pytest

from comet_rag.engines.documents.markdown import MarkdownStructureAnalyzer


def _texts(markdown: str) -> list[str]:
    return [
        markdown[block.start_char : block.end_char]
        for block in MarkdownStructureAnalyzer().analyze(markdown)
    ]


def test_atx_headings_produce_nested_section_paths_and_exact_spans() -> None:
    markdown = (
        "前言。\n\n"
        "# 安装 #\n\n说明。\n\n"
        "### Docker\n\n容器说明。\n\n"
        "## 本地\n\n本地说明。\n\n"
        "# 使用\n\n使用说明。"
    )

    blocks = MarkdownStructureAnalyzer().analyze(markdown)

    assert [block.heading_path for block in blocks] == [
        (),
        ("安装",),
        ("安装", "Docker"),
        ("安装", "本地"),
        ("使用",),
    ]
    assert [block.ordinal for block in blocks] == list(range(5))
    assert "".join(_texts(markdown)) == markdown
    assert all(block.kind == "section" for block in blocks)


@pytest.mark.parametrize("marker", ["```", "~~~~"])
def test_fenced_code_does_not_create_false_headings(marker: str) -> None:
    markdown = (
        f"# 真标题\n\n{marker}python\n# 代码注释\n## 仍是代码\n{marker}\n\n"
        "## 真子标题\n\n正文"
    )

    blocks = MarkdownStructureAnalyzer().analyze(markdown)

    assert [block.heading_path for block in blocks] == [
        ("真标题",),
        ("真标题", "真子标题"),
    ]
    assert "".join(_texts(markdown)) == markdown


def test_setext_headings_participate_in_the_same_hierarchy() -> None:
    markdown = "主标题\n======\n\n正文\n\n子标题\n------\n\n内容"

    blocks = MarkdownStructureAnalyzer().analyze(markdown)

    assert [block.heading_path for block in blocks] == [
        ("主标题",),
        ("主标题", "子标题"),
    ]
    assert _texts(markdown)[1].startswith("子标题\n------")


def test_heading_free_markdown_is_one_section_and_empty_input_has_no_blocks() -> None:
    analyzer = MarkdownStructureAnalyzer()

    blocks = analyzer.analyze("只有正文。")

    assert len(blocks) == 1
    assert blocks[0].heading_path == ()
    assert (blocks[0].start_char, blocks[0].end_char) == (0, 5)
    assert analyzer.analyze("") == ()


def test_repeated_headings_keep_source_coordinates_without_searching_text() -> None:
    markdown = "# 重复\n\n正文\n\n# 重复\n\n正文"

    blocks = MarkdownStructureAnalyzer().analyze(markdown)

    assert len(blocks) == 2
    assert blocks[0].start_char == 0
    assert blocks[1].start_char > blocks[0].end_char - 1
    assert _texts(markdown) == ["# 重复\n\n正文\n\n", "# 重复\n\n正文"]


def test_analyzer_rejects_non_string_input() -> None:
    with pytest.raises(TypeError, match="markdown"):
        MarkdownStructureAnalyzer().analyze(123)  # type: ignore[arg-type]
