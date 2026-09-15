from __future__ import annotations


class DocumentStructureError(ValueError):
    """规范文档的结构事实无法被所选策略安全消费。"""


class MissingDocumentStructureError(DocumentStructureError):
    """所选策略需要的结构事实不存在。"""


__all__ = ["DocumentStructureError", "MissingDocumentStructureError"]
