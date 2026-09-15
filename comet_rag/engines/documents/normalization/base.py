from __future__ import annotations

from typing import Protocol, runtime_checkable

from comet_rag.ports.document import ExtractedDocument, NormalizedDocument


@runtime_checkable
class DocumentNormalizationStrategy(Protocol):
    """把提取结果转换为文档级 ChunkingStrategy 可依赖的稳定表示。"""

    def normalize(self, document: ExtractedDocument) -> NormalizedDocument: ...


__all__ = ["DocumentNormalizationStrategy"]
