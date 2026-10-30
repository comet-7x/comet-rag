from __future__ import annotations

import struct
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from comet_rag.documents.parsers import (
    ArchiveLimits,
    validate_zip_archive,
)
from comet_rag.errors import ArchiveResourceLimitError


def write_archive(
    path: Path, members: dict[str, bytes], *, compression: int = ZIP_STORED
) -> Path:
    with ZipFile(path, "w", compression=compression) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_valid_archive_keeps_original_and_extracts_nothing(tmp_path: Path) -> None:
    path = write_archive(
        tmp_path / "source.docx",
        {
            "word/document.xml": b"<root><text>hello</text></root>",
            "image.bin": b"image",
        },
    )
    original = path.read_bytes()
    validate_zip_archive(path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_compression_budget_is_checked_before_parsing_xml(tmp_path: Path) -> None:
    path = write_archive(
        tmp_path / "compressed.docx",
        {"word/document.xml": b"x" * 100_000},
        compression=ZIP_DEFLATED,
    )
    with pytest.raises(ArchiveResourceLimitError, match="compression ratio"):
        validate_zip_archive(path, ArchiveLimits(max_compression_ratio=5.0))


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (ArchiveLimits(max_members=1), "members"),
        (ArchiveLimits(max_member_uncompressed_bytes=8), "per-member"),
        (ArchiveLimits(max_total_uncompressed_bytes=15), "expands to more"),
        (ArchiveLimits(max_xml_elements=2), "XML.*elements"),
    ],
)
def test_archive_budgets_apply_across_members(
    tmp_path: Path, limits: ArchiveLimits, message: str
) -> None:
    path = write_archive(
        tmp_path / "bounded.docx",
        {"first.xml": b"<root/>", "second.rels": b"<root><cell/></root>"},
    )
    with pytest.raises(ArchiveResourceLimitError, match=message):
        validate_zip_archive(path, limits)


@pytest.mark.parametrize(
    "xml",
    [b"<root>" + b"x" * 64 + b"</root>", b"<root><cell/>" + b"x" * 64 + b"</root>"],
)
def test_xml_text_and_tail_budgets(tmp_path: Path, xml: bytes) -> None:
    path = write_archive(tmp_path / "text.docx", {"document.xml": xml})
    with pytest.raises(ArchiveResourceLimitError, match="text/tail"):
        validate_zip_archive(path, ArchiveLimits(max_xml_text_chars=16))


@pytest.mark.parametrize("ending", [b"</root>", b"<next/></root>"])
def test_xml_tail_budget_spans_parser_read_buffers(
    tmp_path: Path, ending: bytes
) -> None:
    path = write_archive(
        tmp_path / "tail.docx",
        {"document.xml": b"<root><cell/>" + b"x" * 1_000_000 + ending},
    )
    with pytest.raises(ArchiveResourceLimitError, match="text/tail"):
        validate_zip_archive(path, ArchiveLimits(max_xml_text_chars=40_000))


def test_xml_tail_budget_counts_characters_and_allows_exact_boundary(
    tmp_path: Path,
) -> None:
    text = "星" * 40_000
    path = write_archive(
        tmp_path / "tail.docx",
        {"document.xml": f"<root><cell/>{text}<next/>{text}</root>".encode()},
    )
    validate_zip_archive(path, ArchiveLimits(max_xml_text_chars=40_000))
    with pytest.raises(ArchiveResourceLimitError, match="text/tail"):
        validate_zip_archive(path, ArchiveLimits(max_xml_text_chars=39_999))


def test_invalid_zip_and_xml_fail_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "invalid.docx"
    path.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="Invalid ZIP"):
        validate_zip_archive(path)
    write_archive(path, {"word/document.xml": b"<root>"})
    with pytest.raises(ValueError, match="Invalid XML"):
        validate_zip_archive(path)


def test_xml_entities_are_not_loaded(tmp_path: Path) -> None:
    external = tmp_path / "external.txt"
    external.write_text("x" * 128)
    xml = (
        f'<!DOCTYPE root [<!ENTITY data SYSTEM "{external.as_uri()}">]>'
        "<root>&data;</root>"
    ).encode()
    path = write_archive(tmp_path / "entity.docx", {"document.xml": xml})
    validate_zip_archive(path, ArchiveLimits(max_xml_text_chars=16))


def test_encrypted_archive_is_rejected(tmp_path: Path) -> None:
    path = write_archive(tmp_path / "encrypted.docx", {"document.xml": b"<root/>"})
    data = bytearray(path.read_bytes())
    for signature, offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
        flag_offset = data.index(signature) + offset
        flags = struct.unpack_from("<H", data, flag_offset)[0]
        struct.pack_into("<H", data, flag_offset, flags | 1)
    path.write_bytes(data)
    with pytest.raises(ValueError, match="Encrypted archive"):
        validate_zip_archive(path)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_count_limit_requires_positive_integer(value: object) -> None:
    with pytest.raises(ValueError, match="max_members"):
        ArchiveLimits(max_members=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_compression_limit_requires_positive_finite_value(value: object) -> None:
    with pytest.raises(ValueError, match="max_compression_ratio"):
        ArchiveLimits(max_compression_ratio=value)  # type: ignore[arg-type]
