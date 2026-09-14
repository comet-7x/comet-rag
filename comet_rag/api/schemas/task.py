from __future__ import annotations

# 这里刻意不复用 tasks.models.Task：它含 traceback、worker_id 与乐观锁版本，
# 对外只暴露 Task.public_view() 已裁剪的字典。
from typing import Any

from pydantic import BaseModel

#: 直接用 dict 而非逐字段建模：`public_view()` 已经做了裁剪，
#: 再抄一遍字段只会制造两处需要同步维护的真相。
TaskView = dict[str, Any]


class TaskListResponse(BaseModel):
    tasks: list[TaskView]
    total: int
