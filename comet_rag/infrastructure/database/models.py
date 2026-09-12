"""旧 ORM 模型导入路径。"""

from __future__ import annotations

from comet_rag.infrastructure.persistence.sql.models import (
    JsonType,
    KnowledgeBaseRow,
    TaskEventRow,
    TaskRow,
)

__all__ = ["JsonType", "KnowledgeBaseRow", "TaskEventRow", "TaskRow"]
