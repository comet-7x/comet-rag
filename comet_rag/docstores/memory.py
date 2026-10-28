from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from heapq import nsmallest
from typing import cast

from comet_rag.indexing.codec import from_dict, to_dict
from comet_rag.indexing.types import IndexUnit, Representation
from comet_rag.vectorstores.types import VectorConfirmation

from .base import _batch_size, _ids, _parents, _same_manifest, _scope, _snapshot
from .types import WriteManifest, content_digest, manifest_from_dict, manifest_to_dict


class AsyncMemoryDocStore:
    """完整批次原子提交；保存的快照与调用方嵌套数据隔离。"""

    def __init__(self, *, index_scope: str) -> None:
        self._index_scope = _scope(index_scope)
        self._units: dict[str, IndexUnit] = {}
        self._representations: dict[str, Representation] = {}
        self._manifests: dict[str, WriteManifest] = {}

    @property
    def index_scope(self) -> str:
        return self._index_scope

    async def upsert_units(
        self,
        units: Sequence[IndexUnit],
        *,
        representations: Sequence[Representation],
        manifest: WriteManifest,
    ) -> None:
        units, representations, manifest = _snapshot(
            self.index_scope, units, representations, manifest
        )
        for existing in self._manifests.values():
            if (
                existing.source_id == manifest.source_id
                or existing.document_id == manifest.document_id
            ):
                _same_manifest(existing, manifest)
                stored_units = self._read_units(manifest.unit_ids)
                stored_reps = {
                    key: rep
                    for key, rep in self._representations.items()
                    if rep.document_id == manifest.document_id
                }
                if (
                    set(stored_units) != set(manifest.unit_ids)
                    or set(stored_reps) != set(manifest.representation_ids)
                    or content_digest(
                        [stored_units[key] for key in manifest.unit_ids],
                        [stored_reps[key] for key in manifest.representation_ids],
                    )
                    != manifest.content_fingerprint
                ):
                    raise ValueError(
                        "stored manifest content is incomplete or inconsistent"
                    )
                return
        if any(unit.unit_id in self._units for unit in units) or any(
            rep.representation_id in self._representations for rep in representations
        ):
            raise ValueError("content ID already belongs to another manifest")
        # 此段没有 await，验证完成后不会暴露半批次状态。
        self._units.update((unit.unit_id, unit) for unit in units)
        self._representations.update(
            (rep.representation_id, rep) for rep in representations
        )
        self._manifests[manifest.document_id] = manifest

    def _read_units(self, ids: Sequence[str]) -> dict[str, IndexUnit]:
        requested = {key: self._units[key] for key in _ids(ids) if key in self._units}
        relatives = dict(requested)
        pending = list(requested.values())
        while pending:
            parent_id = pending.pop().parent_unit_id
            if parent_id is not None and parent_id not in relatives:
                parent = self._units.get(parent_id)
                if parent is None:
                    raise ValueError("missing parent")
                relatives[parent_id] = parent
                pending.append(parent)
        if any(
            unit.index_scope != self.index_scope or unit.unit_id != key
            for key, unit in relatives.items()
        ):
            raise ValueError("stored unit identity mismatch")
        _parents(relatives)
        return {
            key: cast(IndexUnit, from_dict(to_dict(value)))
            for key, value in requested.items()
        }

    async def get_units(self, ids: Sequence[str]) -> dict[str, IndexUnit]:
        return self._read_units(ids)

    async def get_representations(
        self, ids: Sequence[str]
    ) -> dict[str, Representation]:
        result: dict[str, Representation] = {}
        for key in _ids(ids):
            rep = self._representations.get(key)
            if rep is None:
                continue
            unit = self._read_units((rep.unit_id,)).get(rep.unit_id)
            if (
                unit is None
                or rep.representation_id != key
                or (
                    rep.index_scope,
                    rep.source_id,
                    rep.document_id,
                    rep.index_profile_id,
                )
                != (
                    self.index_scope,
                    unit.source_id,
                    unit.document_id,
                    unit.index_profile_id,
                )
            ):
                raise ValueError("stored representation identity mismatch")
            result[key] = cast(Representation, from_dict(to_dict(rep)))
        return result

    async def get_manifest(self, document_id: str) -> WriteManifest | None:
        _ids((document_id,))
        manifest = self._manifests.get(document_id)
        return (
            manifest_from_dict(manifest_to_dict(manifest))
            if manifest is not None
            else None
        )

    async def iter_manifests(
        self, document_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[WriteManifest]:
        _batch_size(batch_size)
        if document_id is not None:
            manifest = await self.get_manifest(document_id)
            if manifest is not None:
                yield manifest
            return
        after = ""
        while keys := nsmallest(
            batch_size, (key for key in self._manifests if key > after)
        ):
            batch = [
                manifest_from_dict(manifest_to_dict(self._manifests[key]))
                for key in keys
            ]
            for manifest in batch:
                yield manifest
            after = keys[-1]

    async def record_vector_confirmation(
        self, document_id: str, confirmation: VectorConfirmation
    ) -> None:
        _ids((document_id,))
        manifest = self._manifests.get(document_id)
        if manifest is None:
            raise ValueError("manifest does not exist")
        self._manifests[document_id] = manifest.with_confirmation(confirmation)

    async def delete_units(self, ids: Sequence[str]) -> None:
        keys = set(_ids(ids))
        if any(
            unit.parent_unit_id in keys and unit.unit_id not in keys
            for unit in self._units.values()
        ):
            raise ValueError("cannot delete a parent referenced by surviving units")
        self._representations = {
            key: rep
            for key, rep in self._representations.items()
            if rep.unit_id not in keys
        }
        for key in keys:
            self._units.pop(key, None)

    async def delete_manifest(self, document_id: str) -> None:
        _ids((document_id,))
        if any(
            value.document_id == document_id
            for value in (*self._units.values(), *self._representations.values())
        ):
            raise ValueError("cannot delete manifest while content remains")
        self._manifests.pop(document_id, None)
