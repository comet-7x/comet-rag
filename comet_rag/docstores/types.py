from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import Any

from comet_rag.content import text_view
from comet_rag.documents.codec import _encode
from comet_rag.embedder.types import vector_values
from comet_rag.identity import stable_id
from comet_rag.indexing.codec import to_dict
from comet_rag.indexing.types import IndexUnit, Representation, content_fingerprint
from comet_rag.vectorstores.types import VectorConfirmation, VectorRecord


@dataclass(frozen=True, slots=True)
class WriteManifest:
    index_scope: str
    index_profile_id: str
    source_id: str
    document_id: str
    space_id: str
    unit_ids: tuple[str, ...]
    representation_ids: tuple[str, ...]
    record_fingerprints: Mapping[str, str]
    content_fingerprint: str
    record_vectors: Mapping[str, tuple[float, ...]]
    confirmations: tuple[VectorConfirmation, ...] = ()
    manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "index_scope",
            "index_profile_id",
            "source_id",
            "document_id",
            "space_id",
            "content_fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if not value.strip():
                raise ValueError(f"{name} must be nonblank")
        for name in ("unit_ids", "representation_ids"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)):
                raise ValueError("manifest IDs must be unique nonblank strings")
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("manifest IDs must be unique nonblank strings")
            if len(values) != len(set(values)):
                raise ValueError("manifest IDs must be unique nonblank strings")
            object.__setattr__(self, name, tuple(values))
        if not isinstance(self.record_fingerprints, Mapping):
            raise ValueError("record fingerprints must be an ID mapping")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in self.record_fingerprints.items()
        ):
            raise ValueError("record fingerprints must be an ID mapping")
        object.__setattr__(
            self,
            "record_fingerprints",
            MappingProxyType(dict(self.record_fingerprints)),
        )
        if not isinstance(self.record_vectors, Mapping):
            raise ValueError("record vectors must match fingerprint IDs")
        if set(self.record_vectors) != set(self.record_fingerprints):
            raise ValueError("record vectors must match fingerprint IDs")
        vectors: dict[str, tuple[float, ...]] = {}
        for key, values in self.record_vectors.items():
            if not isinstance(values, (list, tuple)):
                raise ValueError("record vectors must be nonempty numeric arrays")
            if not values:
                raise ValueError("record vectors must be nonempty numeric arrays")
            vectors[key] = vector_values(values, len(values))
        object.__setattr__(self, "record_vectors", MappingProxyType(vectors))
        confirmations = self.confirmations
        if not isinstance(confirmations, (list, tuple)):
            raise ValueError("confirmation must reference expected records")
        if any(not isinstance(value, VectorConfirmation) for value in confirmations):
            raise ValueError("confirmation must reference expected records")
        if any(
            not set(value.record_ids) <= set(self.record_ids) for value in confirmations
        ):
            raise ValueError("confirmation must reference expected records")
        object.__setattr__(self, "confirmations", tuple(confirmations))
        object.__setattr__(
            self,
            "manifest_id",
            stable_id(
                "manifest",
                {
                    "scope": self.index_scope,
                    "source": self.source_id,
                    "document": self.document_id,
                    "profile": self.index_profile_id,
                    "space": self.space_id,
                    "units": self.unit_ids,
                    "representations": self.representation_ids,
                    "records": self.record_fingerprints,
                    "vectors": self.record_vectors,
                    "content": self.content_fingerprint,
                },
                version=2,
            ),
        )

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(self.record_fingerprints)

    def with_confirmation(self, confirmation: VectorConfirmation) -> WriteManifest:
        return replace(self, confirmations=(*self.confirmations, confirmation))

    def replay_records(
        self, units: Sequence[IndexUnit], representations: Sequence[Representation]
    ) -> tuple[VectorRecord, ...]:
        by_id = {unit.unit_id: unit for unit in units}
        records: list[VectorRecord] = []
        for rep in representations:
            record_id = stable_id(
                "record",
                {
                    "scope": self.index_scope,
                    "representation": rep.representation_id,
                    "space": self.space_id,
                },
            )
            if rep.unit_id not in by_id:
                raise ValueError("manifest record IDs do not match representations")
            if record_id not in self.record_vectors:
                raise ValueError("manifest record IDs do not match representations")
            record = VectorRecord(
                rep.representation_id,
                rep.unit_id,
                rep.source_id,
                rep.document_id,
                rep.index_scope,
                rep.index_profile_id,
                self.space_id,
                self.record_vectors[record_id],
                text_view(rep.content),
                metadata=by_id[rep.unit_id].metadata,
            )
            if record.fingerprint != self.record_fingerprints[record_id]:
                raise ValueError("stored vectors do not match record fingerprints")
            records.append(record)
        if {record.record_id for record in records} != set(self.record_ids):
            raise ValueError("manifest record IDs do not match representations")
        return tuple(records)


def content_digest(
    units: Sequence[IndexUnit], representations: Sequence[Representation]
) -> str:
    def snapshot(value: IndexUnit | Representation) -> dict[str, Any]:
        result = to_dict(value)
        result["content"] = content_fingerprint(value.content, value.assets)
        # 资源访问位置可迁移；内容哈希和完整结构仍参与清单身份。
        result["assets"] = []
        for asset in value.assets:
            encoded = _encode(asset)
            encoded.pop("media")
            encoded.pop("name")
            result["assets"].append(encoded)
        return result

    return stable_id(
        "stored_content",
        {
            "units": [snapshot(unit) for unit in units],
            "representations": [snapshot(rep) for rep in representations],
        },
    )


def validate_write(
    index_scope: str,
    units: Sequence[IndexUnit],
    representations: Sequence[Representation],
    manifest: WriteManifest,
) -> None:
    if manifest.index_scope != index_scope:
        raise ValueError("manifest scope mismatch")
    if tuple(unit.unit_id for unit in units) != manifest.unit_ids:
        raise ValueError("manifest must enumerate the complete ordered content")
    if (
        tuple(rep.representation_id for rep in representations)
        != manifest.representation_ids
    ):
        raise ValueError("manifest must enumerate the complete ordered content")
    if content_digest(units, representations) != manifest.content_fingerprint:
        raise ValueError("manifest content fingerprint mismatch")
    by_id = {unit.unit_id: unit for unit in units}
    for value in (*units, *representations):
        if (
            value.index_scope,
            value.index_profile_id,
            value.document_id,
            value.source_id,
        ) != (
            index_scope,
            manifest.index_profile_id,
            manifest.document_id,
            manifest.source_id,
        ):
            raise ValueError("content identity mismatch")
    for rep in representations:
        if rep.unit_id not in by_id:
            raise ValueError("representation refers to a missing unit")
    manifest.replay_records(units, representations)
    for unit in units:
        path = {unit.unit_id}
        parent = unit.parent_unit_id
        while parent is not None:
            if parent in path:
                raise ValueError("missing parent or unit cycle")
            if parent not in by_id:
                raise ValueError("missing parent or unit cycle")
            path.add(parent)
            parent = by_id[parent].parent_unit_id


def manifest_to_dict(value: WriteManifest) -> dict[str, Any]:
    return {
        "type": "manifest",
        "schema_version": 2,
        "index_scope": value.index_scope,
        "index_profile_id": value.index_profile_id,
        "source_id": value.source_id,
        "document_id": value.document_id,
        "space_id": value.space_id,
        "unit_ids": list(value.unit_ids),
        "representation_ids": list(value.representation_ids),
        "record_fingerprints": dict(value.record_fingerprints),
        "record_vectors": {
            key: list(vector) for key, vector in value.record_vectors.items()
        },
        "content_fingerprint": value.content_fingerprint,
        "manifest_id": value.manifest_id,
        "confirmations": [
            {
                "record_ids": list(c.record_ids),
                "accepted": c.accepted,
                "durable": c.durable,
                "query_visible": c.query_visible,
                "unknown_ids": list(c.unknown_ids),
                "guarantee": c.guarantee,
            }
            for c in value.confirmations
        ],
    }


def manifest_from_dict(value: Mapping[str, Any]) -> WriteManifest:
    data = dict(value)
    tag = data.pop("type", None)
    version = data.pop("schema_version", None)
    if tag != "manifest":
        raise ValueError("unsupported manifest type/version")
    if type(version) is not int:
        raise ValueError("unsupported manifest type/version")
    if version != 2:
        raise ValueError("unsupported manifest type/version")
    if set(data) != {item.name for item in fields(WriteManifest)}:
        raise ValueError("unknown or missing manifest fields")
    supplied = data.pop("manifest_id")
    for name in ("unit_ids", "representation_ids", "confirmations"):
        if not isinstance(data[name], list):
            raise ValueError(f"{name} must be an array")
    confirmation_fields = {item.name for item in fields(VectorConfirmation)}
    for item in data["confirmations"]:
        if not isinstance(item, dict):
            raise ValueError("unknown or missing confirmation fields")
        if set(item) != confirmation_fields:
            raise ValueError("unknown or missing confirmation fields")
        if any(
            not isinstance(item[name], list) for name in ("record_ids", "unknown_ids")
        ):
            raise ValueError("confirmation IDs must be arrays")
        if not isinstance(item["guarantee"], str):
            raise ValueError("confirmation guarantee must be nonblank")
        if not item["guarantee"].strip():
            raise ValueError("confirmation guarantee must be nonblank")
    try:
        data["confirmations"] = tuple(
            VectorConfirmation(**item) for item in data["confirmations"]
        )
        result = WriteManifest(**data)
    except TypeError as exc:
        raise ValueError("invalid manifest fields") from exc
    if result.manifest_id != supplied:
        raise ValueError("manifest identity mismatch")
    return result
