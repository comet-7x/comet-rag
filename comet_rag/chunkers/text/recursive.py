from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ..types import TextSpan

DEFAULT_SEPARATORS = ("\n\n", "\n", " ", "")


@dataclass(frozen=True, slots=True)
class RecursiveTextChunker:
    """保留空白和分隔符；自然边界 overlap 只复用预算内的完整尾部。"""

    chunk_size: int = 1000
    chunk_overlap: int = 0
    separators: Sequence[str] = DEFAULT_SEPARATORS

    def __post_init__(self) -> None:
        if type(self.chunk_size) is not int:
            raise ValueError("chunk_size must be an integer")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        if type(self.chunk_overlap) is not int:
            raise ValueError("chunk_overlap must be an integer")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("chunk_overlap must satisfy 0 <= overlap < chunk_size")
        if isinstance(self.separators, str):
            raise ValueError("separators must be a sequence of strings")
        if not isinstance(self.separators, Sequence):
            raise ValueError("separators must be a sequence of strings")
        if not self.separators:
            raise ValueError("separators must be nonempty")
        if any(not isinstance(separator, str) for separator in self.separators):
            raise ValueError("separators must contain only strings")
        object.__setattr__(self, "separators", tuple(self.separators))

    @property
    def config(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "type": "recursive_text",
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
                "separators": tuple(self.separators),
                "separator_position": "end",
                "length_unit": "python_characters",
            }
        )

    def split(self, text: str) -> list[TextSpan]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text:
            return []
        return self._split_range(text, TextSpan(0, len(text)), tuple(self.separators))

    def _split_range(
        self, text: str, span: TextSpan, separators: tuple[str, ...]
    ) -> list[TextSpan]:
        if span.end - span.start <= self.chunk_size:
            return [span]
        separator = ""
        remaining: tuple[str, ...] = ()
        for index, candidate in enumerate(separators):
            if not candidate or text.find(candidate, span.start, span.end) >= 0:
                separator = candidate
                remaining = separators[index + 1 :]
                break
        if not separator:
            return self._fixed(span)
        result: list[TextSpan] = []
        ready: list[TextSpan] = []
        cursor = span.start
        pieces: list[TextSpan] = []
        while cursor < span.end:
            found = text.find(separator, cursor, span.end)
            end = span.end if found < 0 else found + len(separator)
            pieces.append(TextSpan(cursor, end))
            cursor = end
        for piece in pieces:
            if piece.end - piece.start <= self.chunk_size:
                ready.append(piece)
            else:
                result.extend(self._merge(ready))
                ready.clear()
                result.extend(self._split_range(text, piece, remaining))
        result.extend(self._merge(ready))
        return result

    def _fixed(self, span: TextSpan) -> list[TextSpan]:
        result: list[TextSpan] = []
        cursor = span.start
        while cursor < span.end:
            end = min(cursor + self.chunk_size, span.end)
            result.append(TextSpan(cursor, end))
            if end == span.end:
                break
            cursor = end - self.chunk_overlap
        return result

    def _merge(self, spans: list[TextSpan]) -> list[TextSpan]:
        result: list[TextSpan] = []
        window: deque[TextSpan] = deque()
        for span in spans:
            if window and span.end - window[0].start > self.chunk_size:
                result.append(TextSpan(window[0].start, window[-1].end))
                while window and (
                    window[-1].end - window[0].start > self.chunk_overlap
                    or span.end - window[0].start > self.chunk_size
                ):
                    window.popleft()
            window.append(span)
        if window:
            result.append(TextSpan(window[0].start, window[-1].end))
        return result
