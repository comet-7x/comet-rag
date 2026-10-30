from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256

from comet_rag.chunkers import SourceRef
from comet_rag.content import ContentInput, ImagePart, content_parts
from comet_rag.documents.schemas import Asset, Block, InlineFormula, Location, _freeze
from comet_rag.identity import stable_id


def _assets(assets: tuple[Asset, ...]) -> None:
    by_id = {asset.asset_id: asset for asset in assets}
    if len(by_id) != len(assets):
        raise ValueError("duplicate asset IDs")
    for asset in assets:
        pending = set(asset.source_asset_ids)
        visited: set[str] = set()
        while pending:
            asset_id = pending.pop()
            if asset_id not in by_id:
                raise ValueError("missing source asset or asset cycle")
            if asset_id == asset.asset_id:
                raise ValueError("missing source asset or asset cycle")
            if asset_id not in visited:
                visited.add(asset_id)
                pending.update(by_id[asset_id].source_asset_ids)


def _blocks(blocks: tuple[Block, ...], assets: tuple[Asset, ...]) -> None:
    by_id = {block.block_id: block for block in blocks}
    if len(by_id) != len(blocks):
        raise ValueError("duplicate block IDs")
    asset_ids = {asset.asset_id for asset in assets}
    order = {block.block_id: index for index, block in enumerate(blocks)}
    owned: set[str] = set()
    for block in blocks:
        path: set[str] = set()
        parent: str | None = block.block_id
        while parent in by_id:
            if parent in path:
                raise ValueError("unit block parent cycle")
            path.add(parent)
            parent = by_id[parent].parent_block_id
        references = [
            block.asset_id,
            block.formula.asset_id if block.formula else None,
            block.table.asset_id if block.table else None,
        ]
        if block.rich_text is not None:
            references.extend(
                inline.formula.asset_id
                for inline in block.rich_text.inlines
                if isinstance(inline, InlineFormula)
            )
        if any(value is not None and value not in asset_ids for value in references):
            raise ValueError("missing block asset")
        if block.table is not None:
            if block.table.cells is not None:
                for cell in block.table.cells:
                    for member in cell.block_ids:
                        if member not in by_id:
                            raise ValueError("missing or invalid table cell block")
                        if by_id[member].parent_block_id != block.block_id:
                            raise ValueError("missing or invalid table cell block")
                        if member in owned:
                            raise ValueError("missing or invalid table cell block")
                        owned.add(member)
                    if list(cell.block_ids) != sorted(
                        cell.block_ids, key=order.__getitem__
                    ):
                        raise ValueError("cell order conflicts with unit block order")
    for block in blocks:
        if block.parent_block_id is None:
            continue
        parent_block = by_id.get(block.parent_block_id)
        if parent_block is None:
            continue
        if parent_block.table is None:
            continue
        if block.block_id not in owned:
            raise ValueError("table child must belong to a cell")


def content_fingerprint(content: ContentInput, assets: tuple[Asset, ...] = ()) -> str:
    parts: list[object] = []
    for part in content_parts(content):
        if isinstance(part, ImagePart):
            matches = {
                asset.content_hash for asset in assets if asset.media == part.resource
            }
            if part.resource.data is not None:
                matches.add(sha256(part.resource.data).hexdigest())
            if len(matches) != 1:
                raise ValueError("image input requires one verified asset content hash")
            parts.append(
                {
                    "type": "image",
                    "sha256": matches.pop(),
                    "media_type": part.resource.media_type,
                }
            )
        else:
            parts.append({"type": "text", "text": part.text})
    return stable_id("content", parts)


def refs_value(refs: tuple[SourceRef, ...]) -> list[dict[str, object]]:
    return [
        {
            "document_id": ref.document_id,
            "block_id": ref.block_id,
            "projection_version": ref.projection_version,
            "start": ref.start,
            "end": ref.end,
        }
        for ref in refs
    ]


@dataclass(frozen=True, slots=True)
class IndexUnit:
    index_scope: str
    index_profile_id: str
    source_id: str
    document_id: str
    content: ContentInput
    source_refs: tuple[SourceRef, ...]
    ordinal: int = 0
    parent_unit_id: str | None = None
    assets: tuple[Asset, ...] = ()
    locations: tuple[Location, ...] = ()
    blocks: tuple[Block, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    unit_id: str = field(init=False)

    def __post_init__(self) -> None:
        for value in (
            self.index_scope,
            self.index_profile_id,
            self.source_id,
            self.document_id,
        ):
            if not isinstance(value, str):
                raise ValueError("unit identity fields must be nonblank")
            if not value.strip():
                raise ValueError("unit identity fields must be nonblank")
        if type(self.ordinal) is not int:
            raise ValueError("ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("ordinal must be nonnegative")
        if self.parent_unit_id is not None:
            if not isinstance(self.parent_unit_id, str):
                raise ValueError("parent ID must be a nonblank string")
            if not self.parent_unit_id.strip():
                raise ValueError("parent ID must be a nonblank string")
        object.__setattr__(self, "content", content_parts(self.content))
        for name, cls in (
            ("source_refs", SourceRef),
            ("assets", Asset),
            ("locations", Location),
            ("blocks", Block),
        ):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)):
                raise ValueError(f"invalid {name}")
            if any(not isinstance(value, cls) for value in values):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, tuple(values))
        if not self.source_refs:
            raise ValueError("unit references must belong to its snapshot")
        if any(ref.document_id != self.document_id for ref in self.source_refs):
            raise ValueError("unit references must belong to its snapshot")
        if len({ref.projection_version for ref in self.source_refs}) > 1:
            raise ValueError("unit references must use the same projection_version")
        _assets(self.assets)
        _blocks(self.blocks, self.assets)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be an object")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        value = {
            "scope": self.index_scope,
            "document": self.document_id,
            "profile": self.index_profile_id,
            "source": self.source_id,
            "refs": refs_value(self.source_refs),
            "ordinal": self.ordinal,
            "parent": self.parent_unit_id,
            "content": content_fingerprint(self.content, self.assets),
        }
        object.__setattr__(self, "unit_id", stable_id("unit", value))


@dataclass(frozen=True, slots=True)
class Representation:
    unit_id: str
    index_scope: str
    index_profile_id: str
    source_id: str
    document_id: str
    content: ContentInput
    purpose: str = "original"
    source_refs: tuple[SourceRef, ...] = ()
    context_refs: tuple[SourceRef, ...] = ()
    assets: tuple[Asset, ...] = ()
    derivation: Mapping[str, object] = field(default_factory=dict)
    representation_id: str = field(init=False)
    content_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for value in (
            self.unit_id,
            self.index_scope,
            self.index_profile_id,
            self.source_id,
            self.document_id,
            self.purpose,
        ):
            if not isinstance(value, str):
                raise ValueError("representation identity fields must be nonblank")
            if not value.strip():
                raise ValueError("representation identity fields must be nonblank")
        object.__setattr__(self, "content", content_parts(self.content))
        for name, cls in (
            ("source_refs", SourceRef),
            ("context_refs", SourceRef),
            ("assets", Asset),
        ):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)):
                raise ValueError(f"invalid {name}")
            if any(not isinstance(value, cls) for value in values):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, tuple(values))
        if any(
            ref.document_id != self.document_id
            for ref in (*self.source_refs, *self.context_refs)
        ):
            raise ValueError("representation references must belong to its snapshot")
        if (
            len(
                {
                    ref.projection_version
                    for ref in (*self.source_refs, *self.context_refs)
                }
            )
            > 1
        ):
            raise ValueError(
                "representation references must use the same projection_version"
            )
        _assets(self.assets)
        if not isinstance(self.derivation, Mapping):
            raise ValueError("derivation must be an object")
        object.__setattr__(self, "derivation", _freeze(self.derivation))
        fingerprint = content_fingerprint(self.content, self.assets)
        object.__setattr__(self, "content_fingerprint", fingerprint)
        object.__setattr__(
            self,
            "representation_id",
            stable_id(
                "representation",
                {
                    "unit": self.unit_id,
                    "scope": self.index_scope,
                    "profile": self.index_profile_id,
                    "source": self.source_id,
                    "document": self.document_id,
                    "purpose": self.purpose,
                    "source_refs": refs_value(self.source_refs),
                    "context_refs": refs_value(self.context_refs),
                    "input": fingerprint,
                    "derivation": self.derivation,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class BuildResult:
    units: tuple[IndexUnit, ...]
    representations: tuple[Representation, ...]
