from __future__ import annotations

from typing import Any

from .archive_guard import ArchiveLimits, ArchiveResourceLimitExceeded
from .base_converter import BaseConverter
from .types import BaseDocument, ByteDocument, DocxDocument

DocxConverter: Any
TextConverter: Any


def __getattr__(name: str) -> Any:
    if name == "DocxConverter":
        from comet_rag.engines.documents.docx.converter import DocxConverter

        globals()[name] = DocxConverter
        return DocxConverter
    if name == "TextConverter":
        from .text_converter import TextConverter

        globals()[name] = TextConverter
        return TextConverter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BaseDocument",
    "ByteDocument",
    "DocxDocument",
    "BaseConverter",
    "TextConverter",
    "DocxConverter",
    "ArchiveLimits",
    "ArchiveResourceLimitExceeded",
]
