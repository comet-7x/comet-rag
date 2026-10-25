from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from comet_rag.engines.chunkers import ChunkDraft, Chunker
from comet_rag.engines.chunkers.strategies import (
    MarkdownSectionStrategy,
    PageChunkingStrategy,
)
from comet_rag.engines.chunkers.strategies._blocks import (
    _validate_coverage,
    _validate_local_draft,
)
from comet_rag.engines.chunkers.strategies.errors import DocumentStructureError
from comet_rag.ports.document import NormalizedDocument


@dataclass(frozen=True, slots=True)
class HierarchyParent:
    ordinal: int
    text: str
    start_char: int
    end_char: int
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        ChunkDraft(
            self.text, self.ordinal, self.start_char, self.end_char, self.metadata
        )
        if len(self.text) != self.end_char - self.start_char:
            raise DocumentStructureError("父块正文长度与 span 不一致")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class HierarchyChild:
    ordinal: int
    parent_ordinal: int
    text: str
    start_char: int
    end_char: int
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        ChunkDraft(
            self.text, self.ordinal, self.start_char, self.end_char, self.metadata
        )
        if type(self.parent_ordinal) is not int or self.parent_ordinal < 0:
            raise ValueError("parent_ordinal 必须是非负整数")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ChunkHierarchy:
    """只有切分事实；父子关系不能跨父块，ordinal 分别连续。"""

    parents: tuple[HierarchyParent, ...]
    children: tuple[HierarchyChild, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parents", tuple(self.parents))
        object.__setattr__(self, "children", tuple(self.children))
        previous_end = 0
        for ordinal, parent in enumerate(self.parents):
            if parent.ordinal != ordinal or parent.start_char < previous_end:
                raise DocumentStructureError("父块 ordinal 必须连续且不重叠")
            if not parent.text.strip():
                raise DocumentStructureError("父块不能只有空白")
            previous_end = parent.end_char
        previous_parent = -1
        previous_start = -1
        for ordinal, child in enumerate(self.children):
            if child.ordinal != ordinal or child.parent_ordinal >= len(self.parents):
                raise DocumentStructureError("子块 ordinal 或父块引用不合法")
            parent = self.parents[child.parent_ordinal]
            if (
                child.parent_ordinal < previous_parent
                or child.start_char < previous_start
            ):
                raise DocumentStructureError("子块必须按文档位置排列")
            if (
                not parent.start_char
                <= child.start_char
                < child.end_char
                <= parent.end_char
            ):
                raise DocumentStructureError("子块不能跨父块")
            start = child.start_char - parent.start_char
            end = child.end_char - parent.start_char
            if parent.text[start:end] != child.text:
                raise DocumentStructureError("子块正文与父块 span 不一致")
            previous_parent = child.parent_ordinal
            previous_start = child.start_char
        referenced = {child.parent_ordinal for child in self.children}
        if referenced != set(range(len(self.parents))):
            raise DocumentStructureError("每个父块必须有可检索子块")


class HierarchyBuilder:
    """先父后子；page/section 是硬边界，父块 overlap 必须为零。"""

    def __init__(self, parent_chunker: Chunker, child_chunker: Chunker) -> None:
        if getattr(parent_chunker, "chunk_overlap", 0) != 0:
            raise ValueError("父块 chunk_overlap 必须为 0")
        self._parent = parent_chunker
        self._child = child_chunker

    def split(self, document: NormalizedDocument, /) -> ChunkHierarchy:
        if not document.blocks:
            drafts = self._parent.split(document.markdown)
        elif all(block.kind == "page" for block in document.blocks):
            drafts = PageChunkingStrategy(self._parent).split(document)
        elif all(block.kind == "section" for block in document.blocks):
            drafts = MarkdownSectionStrategy(self._parent).split(document)
        else:
            raise DocumentStructureError("层级分块只接受统一的 page 或 section blocks")

        parents: list[HierarchyParent] = []
        children: list[HierarchyChild] = []
        if not isinstance(drafts, list):
            raise DocumentStructureError("parent Chunker 必须返回 list[ChunkDraft]")
        parent_spans: list[tuple[int, int]] = []
        for expected, draft in enumerate(drafts):
            start, end = _validate_local_draft(
                draft, expected=expected, source=document.markdown
            )
            parent_spans.append((start, end))
            # 纯空白硬切片没有可回填正文，仍须先验证其 span，不能借此隐藏错误。
            if not draft.text.strip():
                continue
            parent = HierarchyParent(
                len(parents), draft.text, start, end, draft.metadata
            )
            parents.append(parent)
            local_children = self._child.split(parent.text)
            if not isinstance(local_children, list):
                raise DocumentStructureError("child Chunker 必须返回 list[ChunkDraft]")
            child_spans: list[tuple[int, int]] = []
            for local_ordinal, child in enumerate(local_children):
                local_start, local_end = _validate_local_draft(
                    child, expected=local_ordinal, source=parent.text
                )
                child_spans.append((local_start, local_end))
                children.append(
                    HierarchyChild(
                        ordinal=len(children),
                        parent_ordinal=parent.ordinal,
                        text=child.text,
                        start_char=start + local_start,
                        end_char=start + local_end,
                        metadata={**child.metadata, **parent.metadata},
                    )
                )
            _validate_coverage(parent.text, child_spans)
        _validate_coverage(document.markdown, parent_spans)
        return ChunkHierarchy(tuple(parents), tuple(children))
