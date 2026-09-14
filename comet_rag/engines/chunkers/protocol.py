from __future__ import annotations

from typing import Protocol, runtime_checkable

from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.ports.document import NormalizedDocument


@runtime_checkable
class ChunkingStrategy(Protocol):
    """把规范文档切成平坦块的同步纯计算策略。"""

    def split(self, document: NormalizedDocument, /) -> list[ChunkDraft]: ...


__all__ = ["ChunkingStrategy"]
