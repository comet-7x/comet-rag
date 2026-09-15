from __future__ import annotations

from comet_rag.engines.chunkers._length import (
    LengthFunction,
    earliest_suffix_within_budget,
    max_end_within_budget,
    validate_sizing,
)
from comet_rag.engines.chunkers._spans import TextSpan
from comet_rag.engines.chunkers.types import ChunkDraft


class FixedSizeChunker:
    """按可注入长度预算切分，并保留规范 Markdown 的字符位置。"""

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 0,
        *,
        length_function: LengthFunction = len,
    ) -> None:
        validate_sizing(chunk_size, chunk_overlap, length_function)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.length_function = length_function

    def split(self, text: str, /) -> list[ChunkDraft]:
        if not text or not text.strip():
            return []
        return [
            ChunkDraft(
                text=text[span.start : span.end],
                ordinal=ordinal,
                start_char=span.start,
                end_char=span.end,
            )
            for ordinal, span in enumerate(self._split_spans(text, 0, len(text)))
        ]

    def chunk(self, text: str) -> list[str]:
        """只需要正文的便捷入口。"""
        return [draft.text for draft in self.split(text)]

    def _split_spans(self, text: str, start: int, end: int) -> list[TextSpan]:
        spans: list[TextSpan] = []
        cursor = start
        while cursor < end:
            chunk_end = max_end_within_budget(
                text,
                cursor,
                end,
                self.chunk_size,
                self.length_function,
            )
            spans.append(TextSpan(cursor, chunk_end))
            if chunk_end == end:
                break
            next_cursor = earliest_suffix_within_budget(
                text,
                cursor,
                chunk_end,
                self.chunk_overlap,
                self.length_function,
            )
            # 非标准长度函数可能让整个块都落在 overlap 预算内；此时放弃重叠以保证前进。
            cursor = chunk_end if next_cursor <= cursor else next_cursor
        return spans


__all__ = ["FixedSizeChunker"]
