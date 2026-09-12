from __future__ import annotations

from typing import Any

from .base_cleaner import BaseCleaner
from .vision_model import VisionModel

DocxCleaner: Any


def __getattr__(name: str) -> Any:
    if name != "DocxCleaner":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from comet_rag.engines.documents.docx.cleaner import DocxCleaner

    globals()[name] = DocxCleaner
    return DocxCleaner

__all__ = [
    "BaseCleaner",
    "DocxCleaner",
    "VisionModel",
]
