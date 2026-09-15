from __future__ import annotations

import pytest

from comet_rag.engines.chunkers import (
    MARKDOWN_PROFILE,
    CodeLanguage,
    Language,
    RecursiveChunker,
    code_profile,
    language_profile,
)
from comet_rag.engines.chunkers import separators as separator_profiles

# 语言与格式差异只通过 profile 覆盖，不能重新引入参数型 Chunker 子类。


def test_separator_profiles_are_immutable_tuples() -> None:
    constants = {
        name: value
        for name, value in vars(separator_profiles).items()
        if name.startswith("SEPARATORS_")
    }

    assert constants
    assert all(isinstance(value, tuple) for value in constants.values())


@pytest.mark.parametrize("language", list(CodeLanguage))
def test_all_code_profiles_split_without_losing_content(
    language: CodeLanguage,
) -> None:
    source = "\n".join(f"line_{i} = compute(value_{i})" for i in range(60))
    profile = code_profile(language, chunk_size=120, chunk_overlap=0)
    chunks = RecursiveChunker.from_profile(profile).chunk(source)

    assert chunks
    assert "".join(chunks) == source
    assert all(len(chunk) <= 120 for chunk in chunks)


def test_markdown_profile_preserves_a_leading_heading_without_prefix_hacks() -> None:
    text = "# 标题\n\n正文段落。\n\n## 二级标题\n\n更多正文。"
    chunker = RecursiveChunker(
        1000,
        0,
        separators=MARKDOWN_PROFILE.separators,
        separator_position=MARKDOWN_PROFILE.separator_position,
    )

    drafts = chunker.split(text)

    assert "".join(draft.text for draft in drafts) == text
    assert drafts[0].text.startswith("#")
    assert drafts[0].start_char == 0


def test_markdown_profile_preserves_non_heading_prefix() -> None:
    text = "正文先行。\n\n# 后面才有标题\n\n收尾。"
    chunks = RecursiveChunker.from_profile(MARKDOWN_PROFILE).chunk(text)

    assert "".join(chunks) == text


@pytest.mark.parametrize(
    "language", [Language.CHINESE, Language.JAPANESE, Language.KOREAN]
)
def test_cjk_profiles_preserve_content(language: Language) -> None:
    text = "".join(f"这是第{i}个句子。" for i in range(40))
    profile = language_profile(language, chunk_size=60, chunk_overlap=0)
    chunks = RecursiveChunker.from_profile(profile).chunk(text)

    assert "".join(chunks) == text
    assert all(len(chunk) <= 60 for chunk in chunks)


def test_chunks_always_reference_original_text() -> None:
    text = "\n\n".join(f"段落{i}的内容。" for i in range(10))
    chunker = RecursiveChunker(60, separators=("\n\n", "。", ""))

    for draft in chunker.split(text):
        assert draft.start_char is not None
        assert draft.end_char is not None
        assert draft.text == text[draft.start_char : draft.end_char]


def test_overlap_is_best_effort_for_coarse_splits() -> None:
    sentences = " ".join(
        f"Sentence number {i} padded with filler words." for i in range(40)
    )
    profile = language_profile(Language.ENGLISH, chunk_size=100, chunk_overlap=5)

    without = RecursiveChunker(
        100,
        0,
        separators=profile.separators,
    ).chunk(sentences)
    with_overlap = RecursiveChunker.from_profile(profile).chunk(sentences)

    assert with_overlap == without


def test_overlap_materializes_for_character_fallback() -> None:
    blob = "A" * 600
    without = RecursiveChunker(100, 0).chunk(blob)
    with_overlap = RecursiveChunker(100, 30).chunk(blob)

    assert len("".join(with_overlap)) > len("".join(without))


def test_missing_separator_profile_still_honors_overlap() -> None:
    text = "abcdefghij"

    chunks = RecursiveChunker(4, 2, separators=("|",)).chunk(text)

    assert chunks == ["abcd", "cdef", "efgh", "ghij"]
