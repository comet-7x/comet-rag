"""API 出入参模型。

与领域模型分开：`comet_rag/tasks/models.py` 的 `Task` 是内部状态，
这里的是**对外契约**。混用会让内部字段重命名变成破坏性 API 变更。
"""

from .ingest import IngestAccepted, IngestSubmit
from .kb import KnowledgeBaseCreate, KnowledgeBaseInfo
from .search import SearchRequest, SearchResponse, SearchResultItem
from .task import TaskListResponse, TaskView

__all__ = [
    "IngestAccepted",
    "IngestSubmit",
    "KnowledgeBaseCreate",
    "KnowledgeBaseInfo",
    "SearchRequest",
    "SearchResponse",
    "SearchResultItem",
    "TaskListResponse",
    "TaskView",
]
