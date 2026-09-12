from __future__ import annotations

# 不依赖外部服务的文档处理能力。
from .normalization import (
    DocumentNormalizationStrategy,
    MarkdownDocumentNormalizer,
)

__all__ = ["DocumentNormalizationStrategy", "MarkdownDocumentNormalizer"]
