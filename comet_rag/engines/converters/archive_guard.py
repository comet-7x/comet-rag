"""压缩容器在交给高层解析器前的廉价资源预检。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """不解压文件即可从 ZIP central directory 验证的硬上限。"""

    max_members: int = 10_000
    max_member_uncompressed_bytes: int = 64 * 1024 * 1024
    max_total_uncompressed_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_xml_elements: int = 2_000_000
    max_xml_text_chars: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_members <= 0:
            raise ValueError("max_members must be greater than zero")
        if self.max_member_uncompressed_bytes <= 0:
            raise ValueError("max_member_uncompressed_bytes must be greater than zero")
        if self.max_total_uncompressed_bytes <= 0:
            raise ValueError("max_total_uncompressed_bytes must be greater than zero")
        if self.max_compression_ratio <= 0:
            raise ValueError("max_compression_ratio must be greater than zero")
        if self.max_xml_elements <= 0:
            raise ValueError("max_xml_elements must be greater than zero")
        if self.max_xml_text_chars <= 0:
            raise ValueError("max_xml_text_chars must be greater than zero")


class ArchiveResourceLimitExceeded(ValueError):
    """压缩容器的声明规模超过应用允许的输入预算。"""


def validate_zip_archive(
    path: str | Path,
    limits: ArchiveLimits | None = None,
) -> None:
    """在任何解压/DOM 构造前检查 ZIP 的声明资源规模。

    DOCX/PPTX/XLSX 的 central directory 已包含每个成员压缩前后的大小，读取
    它不需要解压 XML，因此能够在攻击者让 lxml/python-docx 分配巨量内存前
    以近似常量内存拒绝压缩炸弹。
    """

    limits = limits or ArchiveLimits()
    archive_path = Path(path)
    try:
        with ZipFile(archive_path) as archive:
            members = archive.infolist()
    except (BadZipFile, OSError) as exc:
        raise ValueError(f"Invalid ZIP-based document: {archive_path}") from exc

    if len(members) > limits.max_members:
        raise ArchiveResourceLimitExceeded(
            f"Archive contains {len(members):,} members; limit is "
            f"{limits.max_members:,}"
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
                f"Archive expands to more than "
                f"{limits.max_total_uncompressed_bytes:,} bytes"
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

    _validate_xml_members(archive_path, members, limits)


def _validate_xml_members(
    archive_path: Path,
    members: list,
    limits: ArchiveLimits,
) -> None:
    """以有界内存扫描 Office XML，限制 DOM 对象数与单文本节点规模。"""

    element_count = 0
    try:
        with ZipFile(archive_path) as archive:
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
                                f"Archive XML contains more than "
                                f"{limits.max_xml_elements:,} elements"
                            )
                        if len(element.text or "") > limits.max_xml_text_chars:
                            raise ArchiveResourceLimitExceeded(
                                f"XML text node in {member.filename!r} exceeds "
                                f"{limits.max_xml_text_chars:,} characters"
                            )
                        # `clear` 加删除已处理兄弟，避免父节点继续持有数百万个
                        # 空壳元素；这是 iterparse 真正保持有界内存的关键。
                        element.clear()
                        parent = element.getparent()
                        if parent is not None:
                            while element.getprevious() is not None:
                                del parent[0]
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Invalid XML member in document archive: {exc}") from exc


__all__ = [
    "ArchiveLimits",
    "ArchiveResourceLimitExceeded",
    "validate_zip_archive",
]
