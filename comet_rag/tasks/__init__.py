"""通用任务框架（产品无关）。

`kind` 是解耦手段：本包不认识 RAG，业务侧注册自己的 runner 即可复用整套
状态机、乐观锁、租约回收与断点续跑。因此本包**不得** import engines/ 或 services/。

分层：
    models   —— Task / TaskStatus / TaskError / StageRecord / TaskEvent（纯数据）
    states   —— 合法状态迁移表（唯一真相）
    store    —— TaskStore（ABC，模板方法）+ InMemoryTaskStore     ← 只管状态
    executor —— TaskExecutor（ABC）+ InProcessExecutor            ← 只管调度
    executor_arq —— ArqExecutor（跨进程）。**不在这里导出**：它会连带 import
                 redis，而只跑单机内存链路的用户不该为此付启动开销。
                 需要时 `from comet_rag.tasks.executor_arq import ArqExecutor`。
    runner   —— TaskContext / Done / register / StagePipeline（业务侧）
    service  —— TaskService（API 层唯一入口）

store 与 executor 的分家是本包最重要的边界：前者回答「这个任务现在怎么样了」
（持久、可查询），后者回答「接下来谁该干活」（短暂、消费即消失）。
合成一个的话，要么队列变得不可查询，要么数据库被当成高频争抢的队列用。
"""

from .executor import InProcessExecutor, StoreDrivenExecutor, TaskExecutor
from .models import (
    StageRecord,
    Task,
    TaskError,
    TaskEvent,
    TaskStatus,
    Time,
)
from .runner import (
    LANE_CPU,
    LANE_IO,
    Done,
    Handoff,
    Outcome,
    RetriableError,
    Runner,
    StagePipeline,
    TaskCancelled,
    TaskContext,
    get_runner,
    register,
    registered_kinds,
    sleep_with_checkpoint,
)
from .service import TaskService
from .states import InvalidTransition, assert_transition, can_transition
from .store import InMemoryTaskStore, TaskBusy, TaskNotFound, TaskStore, VersionConflict

__all__ = [
    "LANE_CPU",
    "LANE_IO",
    "Done",
    "Handoff",
    "InMemoryTaskStore",
    "InProcessExecutor",
    "InvalidTransition",
    "Outcome",
    "RetriableError",
    "Runner",
    "StagePipeline",
    "StageRecord",
    "StoreDrivenExecutor",
    "Task",
    "TaskBusy",
    "TaskCancelled",
    "TaskContext",
    "TaskError",
    "TaskEvent",
    "TaskExecutor",
    "TaskNotFound",
    "TaskService",
    "TaskStatus",
    "TaskStore",
    "Time",
    "VersionConflict",
    "assert_transition",
    "can_transition",
    "get_runner",
    "register",
    "registered_kinds",
    "sleep_with_checkpoint",
]
