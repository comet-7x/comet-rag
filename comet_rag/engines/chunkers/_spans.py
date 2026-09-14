from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextSpan:
    start: int
    end: int


__all__ = ["TextSpan"]
