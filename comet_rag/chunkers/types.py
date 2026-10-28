from __future__ import annotations

from dataclasses import dataclass


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True, slots=True)
class TextSpan:
    """Python 字符偏移；不含终点，且不表示空选择。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        _integer(self.start, "start")
        _integer(self.end, "end", self.start + 1)


@dataclass(frozen=True, slots=True)
class SourceRef:
    document_id: str
    block_id: str
    projection_version: int = 1
    start: int | None = None
    end: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("document_id", self.document_id),
            ("block_id", self.block_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty string")
        _integer(self.projection_version, "projection_version", 1)
        if (self.start is None) != (self.end is None):
            raise ValueError("source range requires both start and end")
        if self.start is not None and self.end is not None:
            TextSpan(self.start, self.end)


@dataclass(frozen=True, slots=True)
class Chunk:
    """text 是快照投影的只读派生视图，不承担内容身份。"""

    document_id: str
    text: str
    source_refs: tuple[SourceRef, ...]
    ordinal: int = 0
    context_refs: tuple[SourceRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not self.document_id:
            raise ValueError("document_id must be a nonempty string")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        _integer(self.ordinal, "ordinal")
        for name in ("source_refs", "context_refs"):
            refs = getattr(self, name)
            if not isinstance(refs, (list, tuple)) or any(
                not isinstance(ref, SourceRef) for ref in refs
            ):
                raise ValueError(f"{name} must contain SourceRef values")
            object.__setattr__(self, name, tuple(refs))
        refs = self.source_refs + self.context_refs
        if any(ref.document_id != self.document_id for ref in refs):
            raise ValueError("references must belong to the same document snapshot")
        if len({ref.projection_version for ref in refs}) > 1:
            raise ValueError("references must use the same projection_version")
