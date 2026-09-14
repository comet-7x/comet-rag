"""M4-T3 平坦分块策略的位置、预算与语言画像契约。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from comet_rag.engines.chunkers import (
    ChunkDraft,
    CodeLanguage,
    CodeRecursiveChunker,
    FixedSizeChunker,
    PythonChunker,
    RecursiveChunker,
)
from comet_rag.ports import NormalizedDocument


def _document(text: str) -> NormalizedDocument:
    return NormalizedDocument(markdown=text)


def _assert_exact_spans(text: str, drafts: list[ChunkDraft]) -> None:
    previous_start = -1
    for ordinal, draft in enumerate(drafts):
        assert draft.ordinal == ordinal
        start = draft.start_char
        end = draft.end_char
        assert start is not None and end is not None
        assert start > previous_start
        assert draft.text == text[start:end]
        previous_start = start


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t \n"])
def test_fixed_and_recursive_ignore_blank_documents(text: str) -> None:
    assert FixedSizeChunker(4).split(_document(text)) == []
    assert RecursiveChunker(4).split(_document(text)) == []


def test_fixed_size_tracks_exact_overlap_positions() -> None:
    text = "0123456789ABCDEFGHIJklmnopqrst"
    drafts = FixedSizeChunker(10, 3).split(_document(text))

    assert [(draft.start_char, draft.end_char) for draft in drafts] == [
        (0, 10),
        (7, 17),
        (14, 24),
        (21, 30),
    ]
    _assert_exact_spans(text, drafts)
    starts = [draft.start_char for draft in drafts]
    ends = [draft.end_char for draft in drafts]
    assert all(start is not None for start in starts)
    assert all(end is not None for end in ends)
    assert [ends[index] - starts[index + 1] for index in range(3)] == [  # type: ignore[operator]
        3,
        3,
        3,
    ]


def test_fixed_zero_overlap_reconstructs_text_including_whitespace_runs() -> None:
    text = "A" + (" " * 18) + "B"
    drafts = FixedSizeChunker(5).split(_document(text))

    assert "".join(draft.text for draft in drafts) == text
    assert any(draft.text.isspace() for draft in drafts)
    _assert_exact_spans(text, drafts)


def test_fixed_supports_an_injected_byte_length_function() -> None:
    def byte_length(value: str) -> int:
        return len(value.encode("utf-8"))

    text = "你好世界"
    drafts = FixedSizeChunker(6, length_function=byte_length).split(_document(text))

    assert [draft.text for draft in drafts] == ["你好", "世界"]
    assert all(byte_length(draft.text) <= 6 for draft in drafts)
    _assert_exact_spans(text, drafts)


@pytest.mark.parametrize(
    ("length_function", "error"),
    [
        (lambda _value: 1, "空字符串"),
        (lambda _value: -1, "负数"),
        (lambda _value: 1.5, "整数"),
    ],
)
def test_length_function_contract_is_validated(
    length_function: Callable[[str], int], error: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        FixedSizeChunker(10, length_function=length_function)


def test_fixed_rejects_a_single_codepoint_over_budget() -> None:
    def byte_length(value: str) -> int:
        return len(value.encode("utf-8"))

    with pytest.raises(ValueError, match="单个字符"):
        FixedSizeChunker(2, length_function=byte_length).chunk("你")


def test_recursive_zero_overlap_reconstructs_repeated_text_and_separators() -> None:
    text = ("重复段落\n\n重复段落\n\n" * 8) + "收尾"
    drafts = RecursiveChunker(13, separators=("\n\n", "\n", "",)).split(
        _document(text)
    )

    assert "".join(draft.text for draft in drafts) == text
    assert all(len(draft.text) <= 13 for draft in drafts)
    assert [draft.start_char for draft in drafts] == [
        0,
        *[draft.end_char for draft in drafts[:-1]],
    ]
    _assert_exact_spans(text, drafts)


def test_recursive_keeps_code_separator_at_the_next_chunk_start() -> None:
    text = (
        "module = True\n"
        "\ndef first():\n    return 1\n"
        "\ndef second():\n    return 2\n"
    )
    drafts = RecursiveChunker(
        40,
        separators=("\ndef ", "\n", " ", ""),
        separator_position="start",
    ).split(_document(text))

    assert "".join(draft.text for draft in drafts) == text
    assert any(draft.text.startswith("\ndef ") for draft in drafts[1:])
    _assert_exact_spans(text, drafts)


def test_recursive_overlap_is_observable_and_best_effort() -> None:
    text = "alpha beta gamma delta epsilon zeta eta theta iota"
    drafts = RecursiveChunker(20, 8, separators=(" ", "")).split(_document(text))

    _assert_exact_spans(text, drafts)
    overlaps = [
        left.end_char - right.start_char  # type: ignore[operator]
        for left, right in zip(drafts, drafts[1:], strict=False)
    ]
    assert all(0 <= overlap <= 8 for overlap in overlaps)
    assert any(overlap > 0 for overlap in overlaps)


@pytest.mark.parametrize("language", list(CodeLanguage))
def test_one_code_recursive_chunker_handles_every_language_profile(
    language: CodeLanguage,
) -> None:
    text = "prefix\n" + ("statement value\n" * 12)
    chunker = CodeRecursiveChunker(language, chunk_size=36, chunk_overlap=0)
    drafts = chunker.split(_document(text))

    assert chunker.code_language is language
    assert "".join(draft.text for draft in drafts) == text
    assert all(len(draft.text) <= 36 for draft in drafts)
    _assert_exact_spans(text, drafts)


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("python", CodeLanguage.PY),
        (".PY", CodeLanguage.PY),
        ("javascript", CodeLanguage.JS),
        ("typescript", CodeLanguage.TS),
        ("c++", CodeLanguage.CPP),
        (".rs", CodeLanguage.RUST),
    ],
)
def test_code_language_accepts_names_and_file_suffixes(
    alias: str, expected: CodeLanguage
) -> None:
    assert CodeRecursiveChunker(alias).code_language is expected


def test_code_language_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="code_language"):
        CodeRecursiveChunker("brainfuck")


def test_legacy_language_class_is_only_a_configuration_facade() -> None:
    text = "head\n\ndef repeated():\n    return 1\n" * 4
    legacy = PythonChunker(chunk_size=48, chunk_overlap=6).split(_document(text))
    unified = CodeRecursiveChunker(
        CodeLanguage.PY, chunk_size=48, chunk_overlap=6
    ).split(_document(text))

    assert legacy == unified


def test_explicit_empty_separator_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="separators"):
        CodeRecursiveChunker(CodeLanguage.GO, separators=())
