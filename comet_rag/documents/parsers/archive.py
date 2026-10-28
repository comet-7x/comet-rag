from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile, ZipInfo

from lxml import etree


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_members: int = 10_000
    max_member_uncompressed_bytes: int = 64 * 1024 * 1024
    max_total_uncompressed_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_xml_elements: int = 2_000_000
    max_xml_text_chars: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_members",
            "max_member_uncompressed_bytes",
            "max_total_uncompressed_bytes",
            "max_xml_elements",
            "max_xml_text_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        ratio = self.max_compression_ratio
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(ratio)
            or ratio <= 0
        ):
            raise ValueError("max_compression_ratio must be positive and finite")


class ArchiveResourceLimitExceeded(ValueError):
    """压缩容器超过允许的资源预算。"""


def validate_zip_archive(
    path: str | Path,
    limits: ArchiveLimits | None = None,
) -> None:
    """先核对 ZIP 声明规模，再扫描 XML；不提取文件或创建完整文档 DOM。"""
    limits = limits or ArchiveLimits()
    archive_path = Path(path)
    try:
        with ZipFile(archive_path) as archive:
            members = archive.infolist()
            _validate_zip_metadata(members, limits)
            _validate_xml_members(archive, members, limits)
    except (BadZipFile, OSError) as exc:
        raise ValueError(f"Invalid ZIP-based document: {archive_path}") from exc


def _validate_zip_metadata(members: list[ZipInfo], limits: ArchiveLimits) -> None:
    if len(members) > limits.max_members:
        raise ArchiveResourceLimitExceeded(
            f"Archive contains {len(members):,} members; limit is {limits.max_members:,}"
        )

    total_uncompressed = 0
    total_compressed = 0
    for member in members:
        if member.flag_bits & 0x1:
            raise ValueError(
                f"Encrypted archive member is not supported: {member.filename}"
            )
        if member.file_size > limits.max_member_uncompressed_bytes:
            raise ArchiveResourceLimitExceeded(
                f"Archive member {member.filename!r} expands to "
                f"{member.file_size:,} bytes; per-member limit is "
                f"{limits.max_member_uncompressed_bytes:,}"
            )
        total_uncompressed += member.file_size
        total_compressed += member.compress_size
        if total_uncompressed > limits.max_total_uncompressed_bytes:
            raise ArchiveResourceLimitExceeded(
                f"Archive expands to more than {limits.max_total_uncompressed_bytes:,} bytes"
            )
        if member.file_size:
            ratio = member.file_size / max(member.compress_size, 1)
            if ratio > limits.max_compression_ratio:
                raise ArchiveResourceLimitExceeded(
                    f"Archive member {member.filename!r} has compression ratio "
                    f"{ratio:.1f}x; limit is {limits.max_compression_ratio:.1f}x"
                )
    if total_uncompressed:
        ratio = total_uncompressed / max(total_compressed, 1)
        if ratio > limits.max_compression_ratio:
            raise ArchiveResourceLimitExceeded(
                f"Archive has overall compression ratio {ratio:.1f}x; limit is "
                f"{limits.max_compression_ratio:.1f}x"
            )


def _validate_xml_members(
    archive: ZipFile,
    members: list[ZipInfo],
    limits: ArchiveLimits,
) -> None:
    element_count = 0
    try:
        for member in members:
            if not member.filename.lower().endswith((".xml", ".rels")):
                continue
            with archive.open(member) as stream:
                context = etree.iterparse(
                    stream,
                    events=("end",),
                    resolve_entities=False,
                    no_network=True,
                    huge_tree=False,
                )
                for _, element in context:
                    element_count += 1
                    if element_count > limits.max_xml_elements:
                        raise ArchiveResourceLimitExceeded(
                            f"Archive XML contains more than {limits.max_xml_elements:,} elements"
                        )
                    text_chars = max(len(element.text or ""), len(element.tail or ""))
                    if text_chars > limits.max_xml_text_chars:
                        raise ArchiveResourceLimitExceeded(
                            f"XML text/tail node in {member.filename!r} exceeds "
                            f"{limits.max_xml_text_chars:,} characters"
                        )
                    # clear 后还要移除已处理的兄弟，避免父节点持有全部空壳。
                    element.clear()
                    parent = element.getparent()
                    if parent is not None:
                        while element.getprevious() is not None:
                            del parent[0]
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Invalid XML member in document archive: {exc}") from exc
