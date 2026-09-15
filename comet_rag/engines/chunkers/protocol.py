from __future__ import annotations

from typing import Protocol, runtime_checkable

from comet_rag.engines.chunkers.types import ChunkDraft
from comet_rag.ports.document import NormalizedDocument


@runtime_checkable
class Chunker(Protocol):
    """把一段文本切成可回引原文的块；实现不需要继承本协议。"""

    def split(self, text: str, /) -> list[ChunkDraft]: ...


@runtime_checkable
class ChunkingStrategy[ResultT](Protocol):
    """基于一个或多个 Chunker 编排完整文档的同步纯计算策略。"""

    def split(self, document: NormalizedDocument, /) -> ResultT: ...


__all__ = ["Chunker", "ChunkingStrategy"]
