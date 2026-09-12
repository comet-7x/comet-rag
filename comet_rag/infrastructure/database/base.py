"""旧 SQLAlchemy 基类导入路径。"""

from __future__ import annotations

from comet_rag.infrastructure.persistence.sql.base import (
    NAMING_CONVENTION,
    Base,
    datetime,
    timestamp_column,
)

__all__ = ["Base", "NAMING_CONVENTION", "datetime", "timestamp_column"]
