"""文档 revision manifest 与父块正文的持久化契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType


def _now() -> datetime:
    return datetime.now(UTC)


def _require_text(
    value: object, field_name: str, *, max_length: int | None = None
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{field_name} 不能超过 {max_length} 个字符")


def _require_int(value: object, field_name: str, *, minimum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} 必须是 int")
    if value < minimum:
        raise ValueError(f"{field_name} 必须大于等于 {minimum}")


def _freeze_metadata(metadata: Mapping[str, object]) -> Mapping[str, object]:
    """隔离嵌套值；只冻结外层，保留内部 dict/list 的类型与内容。"""
    if any(not isinstance(key, str) or not key for key in metadata):
        raise ValueError("metadata 键必须是非空字符串")
    return MappingProxyType(deepcopy(dict(metadata)))


class RevisionStatus(StrEnum):
    STAGING = "staging"
    READY = "ready"
    ACTIVE = "active"
    RETIRED = "retired"
    ABANDONED = "abandoned"
    #: 保留 manifest 作为防复活墓碑；父块和向量已经清理。
    PURGED = "purged"


@dataclass(frozen=True, slots=True)
class RevisionKey:
    kb_id: str
    source_id: str
    revision_id: str

    def __post_init__(self) -> None:
        _require_text(self.kb_id, "RevisionKey.kb_id", max_length=128)
        _require_text(self.source_id, "RevisionKey.source_id", max_length=64)
        _require_text(self.revision_id, "RevisionKey.revision_id", max_length=64)


@dataclass(frozen=True, slots=True)
class RevisionDraft:
    key: RevisionKey
    plan_sha256: str
    parent_count: int
    child_count: int
    source: str
    file_type: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    task_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, RevisionKey):
            raise TypeError("RevisionDraft.key 必须是 RevisionKey")
        if (
            len(self.plan_sha256) != 64
            or self.plan_sha256.lower() != self.plan_sha256
            or any(char not in "0123456789abcdef" for char in self.plan_sha256)
        ):
            raise ValueError("RevisionDraft.plan_sha256 必须是 64 位小写十六进制")
        _require_int(self.parent_count, "RevisionDraft.parent_count", minimum=0)
        _require_int(self.child_count, "RevisionDraft.child_count", minimum=0)
        _require_text(self.source, "RevisionDraft.source")
        _require_text(self.file_type, "RevisionDraft.file_type", max_length=32)
        if self.task_id is not None:
            _require_text(self.task_id, "RevisionDraft.task_id", max_length=64)
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class StoredRevision:
    key: RevisionKey
    generation: int
    status: RevisionStatus
    plan_sha256: str
    parent_count: int
    child_count: int
    task_id: str | None
    replaces_revision_id: str | None
    created_at: datetime
    updated_at: datetime
    ready_at: datetime | None = None
    activated_at: datetime | None = None
    purged_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_int(self.generation, "StoredRevision.generation", minimum=1)
        if not isinstance(self.status, RevisionStatus):
            raise TypeError("StoredRevision.status 必须是 RevisionStatus")


@dataclass(frozen=True, slots=True)
class StoredParent:
    key: RevisionKey
    parent_id: str
    ordinal: int
    text: str
    start_char: int
    end_char: int
    previous_id: str | None = None
    next_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, RevisionKey):
            raise TypeError("StoredParent.key 必须是 RevisionKey")
        _require_text(self.parent_id, "StoredParent.parent_id", max_length=64)
        _require_int(self.ordinal, "StoredParent.ordinal", minimum=0)
        _require_text(self.text, "StoredParent.text")
        _require_int(self.start_char, "StoredParent.start_char", minimum=0)
        _require_int(self.end_char, "StoredParent.end_char", minimum=1)
        if self.end_char <= self.start_char:
            raise ValueError("StoredParent.end_char 必须大于 start_char")
        if self.previous_id is not None:
            _require_text(self.previous_id, "StoredParent.previous_id", max_length=64)
        if self.next_id is not None:
            _require_text(self.next_id, "StoredParent.next_id", max_length=64)
        if self.parent_id in (self.previous_id, self.next_id):
            raise ValueError("StoredParent 不能把自己作为相邻父块")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class Activation:
    active: StoredRevision
    previous_revision_id: str | None
    already_active: bool = False


class DocumentStoreError(RuntimeError):
    """DocumentStore 契约错误的共同边界。"""


class RevisionNotFound(DocumentStoreError):
    def __init__(self, key: RevisionKey) -> None:
        self.key = key
        super().__init__(f"文档 revision 不存在：{key.revision_id!r}")


class RevisionConflict(DocumentStoreError):
    def __init__(self, key: RevisionKey, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"文档 revision {key.revision_id!r} 冲突：{reason}")


class RevisionNotReady(DocumentStoreError):
    def __init__(self, key: RevisionKey, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"文档 revision {key.revision_id!r} 尚不能激活：{reason}")


class StaleRevision(DocumentStoreError):
    def __init__(
        self, key: RevisionKey, *, generation: int, active_generation: int
    ) -> None:
        self.key = key
        self.generation = generation
        self.active_generation = active_generation
        super().__init__(
            f"文档 revision {key.revision_id!r} 的 generation={generation} "
            f"早于当前 active generation={active_generation}"
        )


class ActiveRevisionDeletion(DocumentStoreError):
    def __init__(self, key: RevisionKey) -> None:
        self.key = key
        super().__init__(f"拒绝清理 active revision：{key.revision_id!r}")


class DocumentStore(ABC):
    """文档版本与父块存储；具体实现必须通过共享契约测试。

    写入后的输入与每次读取结果各自持有 metadata 快照，嵌套修改不能回写存储。
    """

    @abstractmethod
    async def reserve_revision(self, draft: RevisionDraft) -> StoredRevision: ...

    @abstractmethod
    async def put_parents(
        self, revision: RevisionKey, parents: Sequence[StoredParent]
    ) -> None: ...

    @abstractmethod
    async def mark_ready(self, revision: RevisionKey) -> StoredRevision: ...

    @abstractmethod
    async def activate_revision(self, revision: RevisionKey) -> Activation: ...

    @abstractmethod
    async def get_active_revisions(
        self, kb_id: str, source_ids: Sequence[str]
    ) -> dict[str, str | None]: ...

    @abstractmethod
    async def get_parents(
        self, kb_id: str, parent_ids: Sequence[str]
    ) -> dict[str, StoredParent]: ...

    @abstractmethod
    async def abandon_revision(self, revision: RevisionKey) -> None: ...

    @abstractmethod
    async def list_cleanup_candidates(self, *, limit: int) -> list[RevisionKey]: ...

    @abstractmethod
    async def purge_inactive_revision(self, revision: RevisionKey) -> None:
        """清除父块并保留 purged manifest，防止旧任务复活。"""

    async def close(self) -> None:
        """释放自身持有的资源；注入的共享 Database 不归适配器关闭。"""
        return None


__all__ = [
    "Activation",
    "ActiveRevisionDeletion",
    "DocumentStore",
    "DocumentStoreError",
    "RevisionConflict",
    "RevisionDraft",
    "RevisionKey",
    "RevisionNotFound",
    "RevisionNotReady",
    "RevisionStatus",
    "StaleRevision",
    "StoredParent",
    "StoredRevision",
]
