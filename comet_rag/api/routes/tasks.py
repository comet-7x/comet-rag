"""任务查询与控制。"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from ...schemas.task import TaskListResponse, TaskView
from ...tasks import TaskStatus
from ..deps import TaskServiceDep

router = APIRouter(prefix="/tasks", tags=["tasks"])

StatusQuery = Annotated[TaskStatus | None, Query(alias="status")]
LimitQuery = Annotated[int, Query(gt=0, le=200)]
OffsetQuery = Annotated[int, Query(ge=0)]
FromScratchQuery = Annotated[
    bool,
    Query(description="true 则整条流水线重来；默认从失败的那个阶段续跑"),
]


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    tasks: TaskServiceDep,
    kind: str | None = None,
    task_status: StatusQuery = None,
    owner_id: str | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> TaskListResponse:
    rows = await tasks.list(
        kind=kind,
        status=task_status,
        owner_id=owner_id,
        limit=limit,
        offset=offset,
    )
    return TaskListResponse(tasks=[t.public_view() for t in rows], total=len(rows))


@router.get("/{task_id}")
async def get_task(task_id: str, tasks: TaskServiceDep) -> TaskView:
    """返回 `public_view()` —— 不外泄 traceback、worker_id、context。"""
    task = await tasks.store.require(task_id)
    return task.public_view()


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_task(task_id: str, tasks: TaskServiceDep) -> dict[str, bool]:
    """**202**：取消是协作式的，受理 ≠ 已停。轮询状态确认真的停了。"""
    return {"accepted": await tasks.cancel(task_id)}


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: str,
    tasks: TaskServiceDep,
    from_scratch: FromScratchQuery = False,
) -> TaskView:
    task = await tasks.retry(task_id, from_scratch=from_scratch)
    return task.public_view()
