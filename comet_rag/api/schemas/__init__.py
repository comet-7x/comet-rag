from __future__ import annotations

# HTTP 出入参属于 API 契约，不与领域模型共用。
from .ingest import IngestAccepted, IngestSubmit
from .kb import KnowledgeBaseCreate, KnowledgeBaseInfo, KnowledgeBaseList
from .search import (
    RetrievalDegradationItem,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from .task import TaskListResponse, TaskView

__all__ = [
    "IngestAccepted",
    "IngestSubmit",
    "KnowledgeBaseCreate",
    "KnowledgeBaseInfo",
    "KnowledgeBaseList",
    "RetrievalDegradationItem",
    "SearchRequest",
    "SearchResponse",
    "SearchResultItem",
    "TaskListResponse",
    "TaskView",
]
