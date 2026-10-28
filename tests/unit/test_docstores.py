from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest

from comet_rag.chunkers import SourceRef
from comet_rag.content import ContentPart, ImagePart, MediaRef, TextPart
from comet_rag.docstores import AsyncDocStore, AsyncMemoryDocStore, WriteManifest
from comet_rag.docstores.types import (
    content_digest,
    manifest_from_dict,
    manifest_to_dict,
)
from comet_rag.documents.schemas import (
    Asset,
    Block,
    RichText,
    TableCell,
    TableContent,
    TextRun,
)
from comet_rag.indexing.types import IndexUnit, Representation
from comet_rag.vectorstores.types import VectorConfirmation, VectorRecord


def write_batch(
    scope: str = "scope-1",
    *,
    document: str = "document-1",
    source: str = "source-1",
    profile: str = "profile-1",
    metadata: dict[str, Any] | None = None,
    asset: Asset | None = None,
) -> tuple[tuple[IndexUnit, ...], tuple[Representation, ...], WriteManifest]:
    refs = (SourceRef(document, "table"), SourceRef(document, "cell"))
    blocks = (
        Block(
            "table",
            "table",
            table=TableContent(cells=(TableCell(0, 0, ("cell",)),), rows=1, columns=1),
        ),
        Block(
            "cell",
            "paragraph",
            rich_text=RichText((TextRun(" 00123 "),)),
            parent_block_id="table",
        ),
    )
    content: list[ContentPart] = [TextPart(" Parent\n 00123 ")]
    if asset is not None:
        content.append(ImagePart(asset.media))
    parent = IndexUnit(
        scope,
        profile,
        source,
        document,
        content,
        refs,
        blocks=blocks,
        assets=(() if asset is None else (asset,)),
        metadata=metadata or {"nested": {"tags": ["original"]}},
    )
    child = IndexUnit(
        scope,
        profile,
        source,
        document,
        " 00123 ",
        (SourceRef(document, "cell", start=0, end=7),),
        ordinal=1,
        parent_unit_id=parent.unit_id,
    )
    rep = Representation(
        child.unit_id,
        scope,
        profile,
        source,
        document,
        " Actual retrieval input \n",
        purpose="summary",
        source_refs=child.source_refs,
        context_refs=parent.source_refs,
        derivation={"generator": {"model": "fixed-model", "options": ["preserve"]}},
    )
    record = VectorRecord(
        rep.representation_id,
        child.unit_id,
        source,
        document,
        scope,
        profile,
        "space-1",
        (1.0, 0.0),
    )
    units = (parent, child)
    reps = (rep,)
    manifest = WriteManifest(
        scope,
        profile,
        source,
        document,
        "space-1",
        tuple(unit.unit_id for unit in units),
        (rep.representation_id,),
        {record.record_id: record.fingerprint},
        content_digest(units, reps),
    )
    return units, reps, manifest


def with_content(
    units: tuple[IndexUnit, ...],
    representations: tuple[Representation, ...],
    manifest: WriteManifest,
) -> WriteManifest:
    return replace(
        manifest,
        unit_ids=tuple(unit.unit_id for unit in units),
        representation_ids=tuple(rep.representation_id for rep in representations),
        content_fingerprint=content_digest(units, representations),
    )


def test_memory_complete_snapshot_id_mapping_and_nested_isolation() -> None:
    async def run() -> None:
        metadata = {"nested": {"tags": ["original"]}}
        units, reps, manifest = write_batch(metadata=metadata)
        store: AsyncDocStore = AsyncMemoryDocStore(index_scope="scope-1")
        await store.upsert_units(units, representations=reps, manifest=manifest)
        metadata["nested"]["tags"].append("changed")
        result = await store.get_units(
            [units[1].unit_id, "missing", units[0].unit_id, units[1].unit_id]
        )
        assert set(result) == {unit.unit_id for unit in units}
        assert result[units[0].unit_id].blocks[1].rich_text == RichText(
            (TextRun(" 00123 "),)
        )
        assert result[units[0].unit_id].metadata == {"nested": {"tags": ("original",)}}
        with pytest.raises(TypeError):
            result[units[0].unit_id].metadata["nested"]["tags"] = ()  # type: ignore[index]
        with pytest.raises(FrozenInstanceError):
            result[units[0].unit_id].ordinal = 5  # type: ignore[misc]
        result.clear()
        assert await store.get_units([units[0].unit_id]) == {units[0].unit_id: units[0]}
        actual = await store.get_representations(["missing", reps[0].representation_id])
        assert actual == {reps[0].representation_id: reps[0]}
        assert actual[reps[0].representation_id].content == (
            TextPart(" Actual retrieval input \n"),
        )
        with pytest.raises(TypeError):
            cast(
                dict[str, Any],
                actual[reps[0].representation_id].derivation["generator"],
            )["model"] = "changed"
        assert await store.get_units([]) == {}
        assert await store.get_representations([]) == {}
        assert await store.get_manifest("absent") is None

    asyncio.run(run())


def test_memory_retries_preserve_confirmations_and_reject_same_scope_changes() -> None:
    async def run() -> None:
        units, reps, manifest = write_batch()
        store = AsyncMemoryDocStore(index_scope="scope-1")
        await store.upsert_units(units, representations=reps, manifest=manifest)
        confirmation = VectorConfirmation(
            manifest.record_ids,
            accepted=True,
            unknown_ids=manifest.record_ids,
            guarantee="request-timeout",
        )
        await store.record_vector_confirmation(manifest.document_id, confirmation)
        await store.upsert_units(units, representations=reps, manifest=manifest)
        saved = await store.get_manifest(manifest.document_id)
        assert saved == manifest.with_confirmation(confirmation)
        assert saved is not manifest
        for other_document, other_profile in (
            ("document-2", "profile-1"),
            ("document-1", "profile-2"),
        ):
            other_units, other_reps, other_manifest = write_batch(
                document=other_document, profile=other_profile
            )
            with pytest.raises(ValueError, match="immutable manifest"):
                await store.upsert_units(
                    other_units, representations=other_reps, manifest=other_manifest
                )
            assert await store.get_units([other_units[0].unit_id]) == {}
        changed_units = (replace(units[0], metadata={"different": True}), units[1])
        assert changed_units[0].unit_id == units[0].unit_id
        changed = with_content(changed_units, reps, manifest)
        with pytest.raises(ValueError, match="immutable manifest"):
            await store.upsert_units(
                changed_units, representations=reps, manifest=changed
            )
        omitted = with_content(
            (units[0],), (), replace(manifest, record_fingerprints={})
        )
        with pytest.raises(ValueError, match="immutable manifest"):
            await store.upsert_units((units[0],), representations=(), manifest=omitted)
        assert await store.get_representations([reps[0].representation_id]) == {
            reps[0].representation_id: reps[0]
        }
        with pytest.raises(ValueError, match="expected records"):
            await store.record_vector_confirmation(
                manifest.document_id, VectorConfirmation(("unrelated",))
            )
        with pytest.raises(ValueError, match="does not exist"):
            await store.record_vector_confirmation("absent", confirmation)
        assert await store.get_manifest(manifest.document_id) == saved

    asyncio.run(run())


def test_memory_invalid_complete_batches_leave_no_content() -> None:
    async def run() -> None:
        units, reps, manifest = write_batch()
        orphan = replace(units[1], parent_unit_id="missing")
        bad_units = (units[0], orphan)
        bad_rep = replace(reps[0], unit_id=orphan.unit_id)
        record = VectorRecord(
            bad_rep.representation_id,
            orphan.unit_id,
            "source-1",
            "document-1",
            "scope-1",
            "profile-1",
            "space-1",
            (1.0, 0.0),
        )
        orphan_manifest = with_content(
            bad_units,
            (bad_rep,),
            replace(
                manifest, record_fingerprints={record.record_id: record.fingerprint}
            ),
        )
        cases = [
            (units, reps, replace(manifest, content_fingerprint="incorrect")),
            (units[:1], reps, manifest),
            (units, (), manifest),
            (units, reps, replace(manifest, record_fingerprints={})),
            (units, reps, replace(manifest, index_scope="another-scope")),
            (units, reps, replace(manifest, source_id="another-source")),
            (bad_units, (bad_rep,), orphan_manifest),
        ]
        for pending_units, pending_reps, pending_manifest in cases:
            store = AsyncMemoryDocStore(index_scope="scope-1")
            with pytest.raises(ValueError):
                await store.upsert_units(
                    pending_units,
                    representations=pending_reps,
                    manifest=pending_manifest,
                )
            assert (
                await store.get_units([unit.unit_id for unit in (*units, orphan)]) == {}
            )
            assert (
                await store.get_representations(
                    [rep.representation_id for rep in (*reps, bad_rep)]
                )
                == {}
            )
            assert [item async for item in store.iter_manifests()] == []

    asyncio.run(run())


def test_memory_exact_deletion_preserves_manifest_other_sources_and_asset(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        path = tmp_path / "shared.png"
        path.write_bytes(b"retained asset")
        asset = Asset("asset", "b" * 64, MediaRef(path=path, media_type="image/png"))
        units, reps, manifest = write_batch(asset=asset)
        other_units, other_reps, other_manifest = write_batch(
            source="other-source", document="other-document"
        )
        store = AsyncMemoryDocStore(index_scope="scope-1")
        await store.upsert_units(units, representations=reps, manifest=manifest)
        await store.upsert_units(
            other_units, representations=other_reps, manifest=other_manifest
        )
        with pytest.raises(ValueError, match="content remains"):
            await store.delete_manifest(manifest.document_id)
        with pytest.raises(ValueError, match="surviving"):
            await store.delete_units([units[0].unit_id])
        assert len(await store.get_units(manifest.unit_ids)) == 2
        await store.delete_units([units[1].unit_id, "absent"])
        assert await store.get_representations(manifest.representation_ids) == {}
        assert await store.get_units(manifest.unit_ids) == {units[0].unit_id: units[0]}
        with pytest.raises(ValueError, match="incomplete"):
            await store.upsert_units(units, representations=reps, manifest=manifest)
        assert await store.get_manifest(manifest.document_id) == manifest
        await store.delete_units([units[0].unit_id])
        await store.delete_manifest(manifest.document_id)
        await store.delete_manifest(manifest.document_id)
        assert await store.get_manifest(manifest.document_id) is None
        assert await store.get_units(other_manifest.unit_ids) == {
            unit.unit_id: unit for unit in other_units
        }
        assert path.read_bytes() == b"retained asset"
        await store.delete_units(other_manifest.unit_ids)
        await store.delete_manifest(other_manifest.document_id)
        assert [item async for item in store.iter_manifests()] == []

    asyncio.run(run())


def test_memory_manifest_pagination_scope_and_argument_guards() -> None:
    async def run() -> None:
        store = AsyncMemoryDocStore(index_scope="scope-1")
        for document in ("doc-c", "doc-a", "doc-b"):
            units, reps, manifest = write_batch(document=document, source=document)
            await store.upsert_units(units, representations=reps, manifest=manifest)
        assert [
            item.document_id async for item in store.iter_manifests(batch_size=1)
        ] == ["doc-a", "doc-b", "doc-c"]
        assert [
            item.document_id
            async for item in store.iter_manifests(document_id="doc-b", batch_size=2)
        ] == ["doc-b"]
        assert [item async for item in store.iter_manifests(document_id="absent")] == []
        for size in (0, -1, True):
            with pytest.raises(ValueError, match="batch_size"):
                _ = [item async for item in store.iter_manifests(batch_size=size)]
        for ids in ([""], [" "], "not-a-sequence-of-IDs"):
            with pytest.raises(ValueError, match="IDs"):
                await store.get_units(ids)
            with pytest.raises(ValueError, match="IDs"):
                await store.get_representations(ids)
            with pytest.raises(ValueError, match="IDs"):
                await store.delete_units(ids)
        with pytest.raises(AttributeError):
            store.index_scope = "changed"  # type: ignore[misc]
        assert (
            await AsyncMemoryDocStore(index_scope="other").get_manifest("doc-a") is None
        )

    with pytest.raises(ValueError, match="index_scope"):
        AsyncMemoryDocStore(index_scope=" ")
    asyncio.run(run())


@pytest.mark.parametrize("name", ["unit_ids", "representation_ids", "confirmations"])
@pytest.mark.parametrize("invalid", ["", {}, None, ()])
def test_manifest_codec_requires_arrays_without_silent_empty_coercion(
    name: str, invalid: Any
) -> None:
    _, _, manifest = write_batch()
    data = manifest_to_dict(manifest)
    data[name] = invalid
    with pytest.raises(ValueError, match="array"):
        manifest_from_dict(data)


@pytest.mark.parametrize(
    "change",
    [
        "missing_units",
        "missing_confirmations",
        "extra",
        "schema_bool",
        "wrong_identity",
        "confirmation_fields",
        "confirmation_ids",
        "confirmation_unknown",
        "confirmation_guarantee",
    ],
)
def test_manifest_codec_rejects_incomplete_or_malformed_payload(change: str) -> None:
    _, _, manifest = write_batch()
    data = manifest_to_dict(
        manifest.with_confirmation(VectorConfirmation(manifest.record_ids))
    )
    if change == "missing_units":
        del data["unit_ids"]
    elif change == "missing_confirmations":
        del data["confirmations"]
    elif change == "extra":
        data["extra"] = "unexpected"
    elif change == "schema_bool":
        data["schema_version"] = True
    elif change == "wrong_identity":
        data["source_id"] = "other"
    elif change == "confirmation_fields":
        data["confirmations"] = [{}]
    elif change == "confirmation_ids":
        data["confirmations"][0]["record_ids"] = ""
    elif change == "confirmation_unknown":
        data["confirmations"][0]["unknown_ids"] = ""
    else:
        data["confirmations"][0]["guarantee"] = {}
    with pytest.raises(ValueError):
        manifest_from_dict(data)


@pytest.mark.parametrize("field", ["unit_ids", "representation_ids", "confirmations"])
def test_manifest_public_sequences_reject_strings_and_keep_defensive_snapshots(
    field: str,
) -> None:
    _, _, manifest = write_batch()
    with pytest.raises(ValueError):
        replace(manifest, **{field: ""})
    if field != "confirmations":
        with pytest.raises(ValueError, match="nonblank"):
            replace(manifest, **{field: (" ",)})
    data = manifest_to_dict(manifest)
    restored = manifest_from_dict(data)
    data["unit_ids"].clear()
    data["record_fingerprints"].clear()
    assert restored == manifest
    assert restored.unit_ids and restored.record_fingerprints


def test_memory_rejects_tampered_self_contained_content_before_persisting_any_rows() -> (
    None
):
    async def run() -> None:
        units, reps, manifest = write_batch()
        parent = deepcopy(manifest_to_dict(manifest))
        broken = replace(units[0])
        object.__setattr__(broken, "blocks", units[0].blocks[:1])
        altered = (broken, units[1])
        changed = replace(manifest, content_fingerprint=content_digest(altered, reps))
        store = AsyncMemoryDocStore(index_scope="scope-1")
        with pytest.raises(ValueError, match="table cell"):
            await store.upsert_units(altered, representations=reps, manifest=changed)
        assert await store.get_units(manifest.unit_ids) == {}
        assert await store.get_representations(manifest.representation_ids) == {}
        assert await store.get_manifest(manifest.document_id) is None
        assert manifest_to_dict(manifest) == parent

    asyncio.run(run())


def test_manifest_content_identity_uses_asset_bytes_and_structure_not_access_paths(
    tmp_path: Path,
) -> None:
    data = b"same image content"
    first = Asset(
        "image",
        sha256(data).hexdigest(),
        MediaRef(data=data, media_type="image/png"),
        name="first.png",
    )
    units, reps, manifest = write_batch(asset=first)
    relocated = replace(
        first,
        media=MediaRef(path=tmp_path / "relocated.png", media_type="image/png"),
        name="second.png",
    )
    moved = replace(
        units[0],
        assets=(relocated,),
        content=tuple(
            ImagePart(relocated.media) if isinstance(part, ImagePart) else part
            for part in units[0].content
        ),
    )
    assert moved.unit_id == units[0].unit_id
    assert content_digest((moved, units[1]), reps) == manifest.content_fingerprint
    assert (
        replace(
            manifest, content_fingerprint=content_digest((moved, units[1]), reps)
        ).manifest_id
        == manifest.manifest_id
    )
    changed_block = replace(
        units[0].blocks[1], rich_text=RichText((TextRun("different returned cell"),))
    )
    changed = replace(units[0], blocks=(units[0].blocks[0], changed_block))
    assert content_digest((changed, units[1]), reps) != manifest.content_fingerprint
    changed = replace(units[0], metadata={"nested": {"tags": ["changed"]}})
    assert content_digest((changed, units[1]), reps) != manifest.content_fingerprint


def test_memory_detects_dangling_or_cross_document_parent() -> None:
    async def run() -> None:
        units, reps, manifest = write_batch()
        store = AsyncMemoryDocStore(index_scope="scope-1")
        await store.upsert_units(units, representations=reps, manifest=manifest)
        store._units.pop(units[0].unit_id)
        with pytest.raises(ValueError, match="missing parent"):
            await store.get_units([units[1].unit_id])
        with pytest.raises(ValueError, match="missing parent"):
            await store.get_representations(manifest.representation_ids)
        other_parent = replace(
            units[0], document_id="other", source_refs=(SourceRef("other", "table"),)
        )
        store._units[units[0].unit_id] = other_parent
        with pytest.raises(ValueError, match="identity mismatch"):
            await store.get_units([units[1].unit_id])

    asyncio.run(run())
