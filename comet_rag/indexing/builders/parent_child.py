from __future__ import annotations

from dataclasses import replace

from comet_rag.chunkers import SourceRef
from comet_rag.documents.schemas import ParsedDocument
from comet_rag.embedder.types import EmbeddingSpace
from comet_rag.identity import stable_id

from ..types import BuildResult, IndexUnit
from .flat import FlatIndexBuilder


def _contains(parent: SourceRef, child: SourceRef) -> bool:
    if (parent.document_id, parent.block_id, parent.projection_version) != (
        child.document_id,
        child.block_id,
        child.projection_version,
    ):
        return False
    if parent.start is None:
        return True
    return (
        child.start is not None
        and child.end is not None
        and parent.end is not None
        and parent.start <= child.start <= child.end <= parent.end
    )


class ParentChildIndexBuilder:
    """仅子单元建立表示；父内容独立保存，子范围必须被唯一父范围完整包含。"""

    def __init__(self, *, parent: FlatIndexBuilder, child: FlatIndexBuilder) -> None:
        self.parent = parent
        self.child = child

    def profile_id(self, space: EmbeddingSpace) -> str:
        return stable_id(
            "profile",
            {
                "builder": "parent_child_v1",
                "parent": self.parent.profile_id(space),
                "child": self.child.profile_id(space),
            },
        )

    def build(
        self, document: ParsedDocument, *, index_scope: str, index_profile_id: str
    ) -> BuildResult:
        parents = self.parent.build(
            document, index_scope=index_scope, index_profile_id=index_profile_id
        )
        children = self.child.build(
            document, index_scope=index_scope, index_profile_id=index_profile_id
        )
        parent_units = parents.units
        materialized: list[IndexUnit] = []
        by_previous: dict[str, IndexUnit] = {}
        for original in children.units:
            matches = [
                parent
                for parent in parent_units
                if all(
                    any(_contains(parent_ref, ref) for parent_ref in parent.source_refs)
                    for ref in original.source_refs
                )
            ]
            if len(matches) != 1:
                raise ValueError(
                    "child range requires exactly one containing parent; align chunk boundaries and disable parent overlap"
                )
            child = replace(
                original,
                parent_unit_id=matches[0].unit_id,
                ordinal=len(parent_units) + original.ordinal,
            )
            by_previous[original.unit_id] = child
            materialized.append(child)
        representations = tuple(
            replace(rep, unit_id=by_previous[rep.unit_id].unit_id)
            for rep in children.representations
        )
        return BuildResult((*parent_units, *materialized), representations)
