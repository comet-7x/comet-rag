"""与产品无关的持久化任务框架。"""

from .executor import InProcessExecutor, StoreDrivenExecutor, TaskExecutor
from .models import (
    StageRecord,
    Task,
    TaskError,
    TaskEvent,
    TaskStatus,
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
from .store import TaskBusy, TaskNotFound, TaskStore, VersionConflict
from .store_memory import InMemoryTaskStore

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
    "VersionConflict",
    "assert_transition",
    "can_transition",
    "get_runner",
    "register",
    "registered_kinds",
    "sleep_with_checkpoint",
]
