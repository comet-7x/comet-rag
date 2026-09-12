"""PostgreSQL TaskStore 的旧导入路径。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["PostgresTaskStore"]
PostgresTaskStore: Any


def __getattr__(name: str) -> Any:
    if name != "PostgresTaskStore":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = import_module(
        "comet_rag.infrastructure.persistence.task_store.postgres"
    ).PostgresTaskStore
    globals()[name] = value
    return value
