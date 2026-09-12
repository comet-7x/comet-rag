from __future__ import annotations

from typing import Any

from .base_parser import BaseParser
from .types import BaseParsedContent, DocxParsedContent

DocxParser: Any


def __getattr__(name: str) -> Any:
    if name != "DocxParser":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from comet_rag.engines.documents.docx.parser import DocxParser

    globals()[name] = DocxParser
    return DocxParser

__all__ = [
    "BaseParsedContent",
    "DocxParsedContent",
    "BaseParser",
    "DocxParser",
]
