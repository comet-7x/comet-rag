from __future__ import annotations

import pytest

from comet_rag.engines.chunkers import (
    CSV_PROFILE,
    DOCX_PROFILE,
    JSON_PROFILE,
    MARKDOWN_PROFILE,
    TEXT_PROFILE,
    XML_PROFILE,
    ChunkProfile,
    RecursiveChunker,
)

# 所有参数画像共享同一套递归算法，也必须共享同一套分块不变式。

PROFILES = [
    pytest.param(TEXT_PROFILE, id="text"),
    pytest.param(DOCX_PROFILE, id="docx"),
    pytest.param(MARKDOWN_PROFILE, id="markdown"),
    pytest.param(CSV_PROFILE, id="csv"),
    pytest.param(JSON_PROFILE, id="json"),
    pytest.param(XML_PROFILE, id="xml"),
]

PROSE = "\n\n".join(
    " ".join(f"Paragraph {p} sentence {s} with several words." for s in range(6))
    for p in range(12)
)


def _chunker(
    profile: ChunkProfile,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> RecursiveChunker:
    return RecursiveChunker(
        chunk_size,
        chunk_overlap,
        separators=profile.separators,
        separator_position=profile.separator_position,
    )


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("chunk_size", [40, 120, 400])
def test_no_chunk_exceeds_chunk_size(
    profile: ChunkProfile, chunk_size: int
) -> None:
    chunks = _chunker(
        profile, chunk_size=chunk_size, chunk_overlap=0
    ).chunk(PROSE)

    assert all(len(chunk) <= chunk_size for chunk in chunks)


@pytest.mark.parametrize("profile", PROFILES)
def test_zero_overlap_reconstructs_text_exactly(profile: ChunkProfile) -> None:
    chunks = _chunker(profile, chunk_size=120, chunk_overlap=0).chunk(PROSE)

    assert "".join(chunks) == PROSE


@pytest.mark.parametrize("profile", PROFILES)
def test_no_empty_chunks(profile: ChunkProfile) -> None:
    chunks = _chunker(profile, chunk_size=60, chunk_overlap=10).chunk(PROSE)

    assert chunks
    assert all(chunk for chunk in chunks)


@pytest.mark.parametrize("profile", PROFILES)
def test_chunks_are_ordered_substrings(profile: ChunkProfile) -> None:
    chunks = _chunker(profile, chunk_size=120, chunk_overlap=0).chunk(PROSE)

    cursor = 0
    for chunk in chunks:
        found = PROSE.find(chunk, cursor)
        assert found >= 0, f"chunk 不是原文子串：{chunk[:40]!r}"
        cursor = found


@pytest.mark.parametrize("profile", PROFILES)
def test_overlap_materializes_for_character_fallback(profile: ChunkProfile) -> None:
    blob = "A" * 600
    without = _chunker(profile, chunk_size=100, chunk_overlap=0).chunk(blob)
    with_overlap = _chunker(profile, chunk_size=100, chunk_overlap=30).chunk(blob)

    assert len("".join(with_overlap)) > len("".join(without))


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t \n"])
def test_blank_input_yields_nothing(profile: ChunkProfile, text: str) -> None:
    assert _chunker(profile, chunk_size=100, chunk_overlap=10).chunk(text) == []


@pytest.mark.parametrize("profile", PROFILES)
def test_short_and_single_character_inputs(profile: ChunkProfile) -> None:
    chunker = _chunker(profile, chunk_size=100, chunk_overlap=10)

    assert chunker.chunk("x") == ["x"]
    assert chunker.chunk("短文本，不该被切开。") == ["短文本，不该被切开。"]


@pytest.mark.parametrize("profile", PROFILES)
def test_long_text_without_matching_separator_uses_fixed_fallback(
    profile: ChunkProfile,
) -> None:
    blob = "A" * 500
    chunks = _chunker(profile, chunk_size=100, chunk_overlap=0).chunk(blob)

    assert chunks == ["A" * 100] * 5


@pytest.mark.parametrize("profile", PROFILES)
def test_invalid_sizing_is_rejected(profile: ChunkProfile) -> None:
    with pytest.raises(ValueError, match="chunk_overlap"):
        _chunker(profile, chunk_size=50, chunk_overlap=50)
    with pytest.raises(ValueError, match="chunk_overlap"):
        _chunker(profile, chunk_size=50, chunk_overlap=100)


@pytest.mark.parametrize("profile", PROFILES)
def test_profile_defaults_are_applied_without_new_chunker_types(
    profile: ChunkProfile,
) -> None:
    chunker = RecursiveChunker.from_profile(profile)
    text = "x" * (profile.chunk_size * 2)
    chunks = chunker.chunk(text)

    assert chunker.chunk_size == profile.chunk_size
    assert chunker.chunk_overlap == profile.chunk_overlap
    assert chunker.separators == profile.separators
    assert all(len(chunk) <= profile.chunk_size for chunk in chunks)
    assert len("".join(chunks)) > len(text)
