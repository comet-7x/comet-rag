"""ORM 模型。

**新增模型必须定义在本模块（或被本模块 import）**，否则
`alembic revision --autogenerate` 收集不到它，会误判为"这张表该删掉"。

T19 在这里加 `knowledge_bases`，T20 加 `tasks` 与 `task_events`。
"""

from __future__ import annotations

__all__: list[str] = []
