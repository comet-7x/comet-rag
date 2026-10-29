from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast

import pytest

from comet_rag.content import MediaRef
from comet_rag.docstores import AsyncMemoryDocStore
from comet_rag.docstores.base import _content_payload
from comet_rag.docstores.postgres import AsyncPostgresDocStore
from comet_rag.docstores.types import content_digest
from comet_rag.documents.schemas import Asset, RichText, TextRun
from comet_rag.indexing.codec import from_dict, to_dict
from comet_rag.indexing.types import IndexUnit, Representation
from tests.unit.test_docstores import write_batch


def envelope(value: IndexUnit | Representation) -> dict[str, Any]:
    return {
        **to_dict(value),
        "snapshot_version": 1,
        "snapshot_fingerprint": (
            content_digest((value,), ())
            if isinstance(value, IndexUnit)
            else content_digest((), (value,))
        ),
    }


@pytest.mark.parametrize("corruption", ["blocks", "metadata"])
@pytest.mark.parametrize("read", ["parent", "child", "representation"])
def test_memory_reads_reject_identity_preserving_snapshot_corruption(
    corruption: str, read: str
) -> None:
    async def run() -> None:
        units, reps, manifest = write_batch()
        store = AsyncMemoryDocStore(index_scope=manifest.index_scope)
        await store.upsert_units(units, representations=reps, manifest=manifest)
        if corruption == "blocks":
            block = replace(
                units[0].blocks[1], rich_text=RichText((TextRun("corrupt cell"),))
            )
            changed = replace(units[0], blocks=(units[0].blocks[0], block))
        else:
            changed = replace(units[0], metadata={"corrupt": True})
        assert changed.unit_id == units[0].unit_id
        store._units[changed.unit_id] = changed
        with pytest.raises(ValueError, match="snapshot fingerprint"):
            if read == "representation":
                await store.get_representations([reps[0].representation_id])
            else:
                await store.get_units([units[1 if read == "child" else 0].unit_id])

    asyncio.run(run())


def test_memory_reads_reject_identity_preserving_representation_corruption() -> None:
    async def run() -> None:
        units, reps, manifest = write_batch()
        store = AsyncMemoryDocStore(index_scope=manifest.index_scope)
        await store.upsert_units(units, representations=reps, manifest=manifest)
        changed = replace(
            reps[0], assets=(Asset("extra", "a" * 64, MediaRef(path="stable.png")),)
        )
        assert changed.representation_id == reps[0].representation_id
        store._representations[changed.representation_id] = changed
        with pytest.raises(ValueError, match="snapshot fingerprint"):
            await store.get_representations([changed.representation_id])
        with pytest.raises(ValueError, match="snapshot fingerprint"):
            await store.upsert_units(units, representations=reps, manifest=manifest)

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["unit", "representation"])
@pytest.mark.parametrize("fingerprint", [None, "incorrect"])
def test_memory_rejects_missing_or_corrupt_snapshot_fingerprint(
    kind: str, fingerprint: str | None
) -> None:
    async def run() -> None:
        units, reps, manifest = write_batch()
        store = AsyncMemoryDocStore(index_scope=manifest.index_scope)
        await store.upsert_units(units, representations=reps, manifest=manifest)
        fingerprints = (
            store._unit_fingerprints
            if kind == "unit"
            else store._representation_fingerprints
        )
        key = units[0].unit_id if kind == "unit" else reps[0].representation_id
        if fingerprint is None:
            fingerprints.pop(key)
        else:
            fingerprints[key] = fingerprint
        with pytest.raises(ValueError, match="snapshot fingerprint|new index_scope"):
            if kind == "unit":
                await store.get_units([key])
            else:
                await store.get_representations([key])

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["unit", "representation"])
@pytest.mark.parametrize(
    "corruption", ["none", "metadata", "blocks", "missing", "fingerprint", "version"]
)
def test_postgres_decode_validates_snapshot_envelope_offline(
    kind: str, corruption: str
) -> None:
    units, reps, _ = write_batch()
    value = (
        units[0]
        if kind == "unit"
        else replace(
            reps[0],
            assets=(Asset("extra", "a" * 64, MediaRef(path="stable.png")),),
        )
    )
    payload = envelope(value)
    assert _content_payload(value) == payload
    if corruption == "metadata":
        if kind == "unit":
            payload["metadata"] = {"changed": True}
        else:
            payload["assets"][0]["provenance"] = "corrupt"
    elif corruption == "blocks":
        if kind == "unit":
            payload["blocks"][1]["rich_text"]["inlines"][0]["text"] = "corrupt cell"
        else:
            payload["assets"][0]["content_hash"] = "b" * 64
    elif corruption == "missing":
        del payload["snapshot_version"]
        del payload["snapshot_fingerprint"]
    elif corruption == "fingerprint":
        payload["snapshot_fingerprint"] = "corrupt"
    elif corruption == "version":
        payload["snapshot_version"] = True
    if corruption in ("metadata", "blocks"):
        bare = {
            key: item
            for key, item in payload.items()
            if key not in ("snapshot_version", "snapshot_fingerprint")
        }
        decoded = from_dict(bare)
        assert type(decoded) is type(value)
        assert decoded.unit_id == value.unit_id
        if isinstance(decoded, Representation):
            assert isinstance(value, Representation)
            assert decoded.representation_id == value.representation_id
    row = to_dict(value)
    row["payload"] = payload
    store = AsyncPostgresDocStore(index_scope=value.index_scope, engine=cast(Any, None))
    cls = IndexUnit if kind == "unit" else Representation
    if corruption == "none":
        assert store._decode(cast(Any, row), cls) == value
        assert row["payload"] == payload
    else:
        with pytest.raises(ValueError, match="snapshot fingerprint|new index_scope"):
            store._decode(cast(Any, row), cls)


def test_memory_partial_reads_and_deletion_keep_snapshot_verification() -> None:
    async def run() -> None:
        units, reps, manifest = write_batch()
        store = AsyncMemoryDocStore(index_scope=manifest.index_scope)
        await store.upsert_units(units, representations=reps, manifest=manifest)
        assert await store.get_units([units[1].unit_id]) == {units[1].unit_id: units[1]}
        assert await store.get_representations([reps[0].representation_id]) == {
            reps[0].representation_id: reps[0]
        }
        await store.delete_units([units[1].unit_id])
        assert await store.get_units([units[0].unit_id]) == {units[0].unit_id: units[0]}
        store._units[units[0].unit_id] = replace(units[0], metadata={"changed": True})
        with pytest.raises(ValueError, match="snapshot fingerprint"):
            await store.get_units([units[0].unit_id])
        assert await store.get_manifest(manifest.document_id) == manifest

    asyncio.run(run())
