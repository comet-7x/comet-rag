"""任务接口出参。

刻意不复用 `comet_rag/tasks/models.py::Task`：那是内部状态，含 traceback、
worker_id、乐观锁版本号等不该外泄的字段。对外只返回 `Task.public_view()`
的裁剪结果。
"""

from typing import Any

from pydantic import BaseModel

#: 直接用 dict 而非逐字段建模：`public_view()` 已经做了裁剪，
#: 再抄一遍字段只会制造两处需要同步维护的真相。
TaskView = dict[str, Any]


class TaskListResponse(BaseModel):
    tasks: list[TaskView]
    total: int
