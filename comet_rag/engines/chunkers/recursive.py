from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from comet_rag.engines.chunkers._length import (
    LengthFunction,
    measure,
    validate_sizing,
)
from comet_rag.engines.chunkers._spans import TextSpan
from comet_rag.engines.chunkers.fixed import FixedSizeChunker
from comet_rag.engines.chunkers.profiles import ChunkProfile, SeparatorPosition
from comet_rag.engines.chunkers.types import ChunkDraft

DEFAULT_SEPARATORS = ("\n\n", "\n", " ", "")


class RecursiveChunker:
    """按分隔符优先级递归切分，字符 span 在整个过程中保持可追溯。"""

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 0,
        *,
        separators: Sequence[str] | None = None,
        separator_position: SeparatorPosition = "end",
        length_function: LengthFunction = len,
    ) -> None:
        validate_sizing(chunk_size, chunk_overlap, length_function)
        selected = tuple(separators) if separators is not None else DEFAULT_SEPARATORS
        if not selected:
            raise ValueError("separators 不能为空")
        if any(not isinstance(separator, str) for separator in selected):
            raise TypeError("separators 中的每一项都必须是字符串")
        if separator_position not in ("start", "end"):
            raise ValueError("separator_position 只能是 'start' 或 'end'")

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = selected
        self.separator_position = separator_position
        self.length_function = length_function
        self._fixed = FixedSizeChunker(
            chunk_size,
            chunk_overlap,
            length_function=length_function,
        )

    @classmethod
    def from_profile(
        cls,
        profile: ChunkProfile,
        /,
        *,
        length_function: LengthFunction = len,
    ) -> RecursiveChunker:
        return cls(
            profile.chunk_size,
            profile.chunk_overlap,
            separators=profile.separators,
            separator_position=profile.separator_position,
            length_function=length_function,
        )

    def split(self, text: str, /) -> list[ChunkDraft]:
        if not text or not text.strip():
            return []
        spans = self._split_range(text, TextSpan(0, len(text)), self.separators)
        return [
            ChunkDraft(
                text=text[span.start : span.end],
                ordinal=ordinal,
                start_char=span.start,
                end_char=span.end,
            )
            for ordinal, span in enumerate(spans)
        ]

    def chunk(self, text: str) -> list[str]:
        """只需要正文的便捷入口。"""
        return [draft.text for draft in self.split(text)]

    def _split_range(
        self,
        text: str,
        span: TextSpan,
        separators: tuple[str, ...],
    ) -> list[TextSpan]:
        if measure(self.length_function, text[span.start : span.end]) <= self.chunk_size:
            return [span]

        separator, remaining = self._select_separator(text, span, separators)
        if not separator:
            return self._fixed._split_spans(text, span.start, span.end)

        pieces = self._split_on_separator(text, span, separator)
        result: list[TextSpan] = []
        ready: list[TextSpan] = []
        for piece in pieces:
            if measure(self.length_function, text[piece.start : piece.end]) <= self.chunk_size:
                ready.append(piece)
                continue

            if ready:
                result.extend(self._merge_spans(text, ready))
                ready = []
            if remaining:
                result.extend(self._split_range(text, piece, remaining))
            else:
                result.extend(self._fixed._split_spans(text, piece.start, piece.end))

        if ready:
            result.extend(self._merge_spans(text, ready))
        return result

    @staticmethod
    def _select_separator(
        text: str,
        span: TextSpan,
        separators: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        value = text[span.start : span.end]
        for index, separator in enumerate(separators):
            if not separator or separator in value:
                return separator, separators[index + 1 :]
        return "", ()

    def _split_on_separator(
        self,
        text: str,
        span: TextSpan,
        separator: str,
    ) -> list[TextSpan]:
        positions: list[int] = []
        cursor = span.start
        while True:
            found = text.find(separator, cursor, span.end)
            if found < 0:
                break
            positions.append(found)
            cursor = found + len(separator)

        if not positions:
            return [span]
        if self.separator_position == "start":
            boundaries = [span.start, *positions, span.end]
        else:
            boundaries = [
                span.start,
                *(position + len(separator) for position in positions),
                span.end,
            ]
        return [
            TextSpan(start, end)
            for start, end in zip(boundaries, boundaries[1:], strict=False)
            if start < end
        ]

    def _merge_spans(self, text: str, spans: list[TextSpan]) -> list[TextSpan]:
        merged: list[TextSpan] = []
        window: deque[TextSpan] = deque()

        for span in spans:
            if window and self._measure_range(text, window[0].start, span.end) > self.chunk_size:
                merged.append(TextSpan(window[0].start, window[-1].end))
                while window and (
                    self._measure_range(text, window[0].start, window[-1].end)
                    > self.chunk_overlap
                    or self._measure_range(text, window[0].start, span.end)
                    > self.chunk_size
                ):
                    window.popleft()
            window.append(span)

        if window:
            merged.append(TextSpan(window[0].start, window[-1].end))
        return merged

    def _measure_range(self, text: str, start: int, end: int) -> int:
        return measure(self.length_function, text[start:end])


__all__ = ["DEFAULT_SEPARATORS", "RecursiveChunker", "SeparatorPosition"]
