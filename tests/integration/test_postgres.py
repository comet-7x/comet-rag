from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from comet_rag.content import MediaRef, TextPart, text_view
from comet_rag.docstores.base import _content_payload
from comet_rag.docstores.postgres import AsyncPostgresDocStore
from comet_rag.docstores.types import manifest_to_dict
from comet_rag.documents.schemas import Asset, RichText, TextRun
from comet_rag.vectorstores.types import VectorConfirmation, VectorRecord
from tests.unit.test_docstores import with_content, write_batch

pytestmark = pytest.mark.integration


def engine() -> AsyncEngine:
    # 集成调用方显式提供专用测试连接；缺后端或配置时直接失败。
    return create_async_engine(os.environ["COMET_RAG_TEST_POSTGRES_URL"])


def scope() -> str:
    return f"v2it{uuid4().hex}"


def test_postgres_persists_content_confirmations_and_exact_deletion(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        index_scope, other_scope = scope(), scope()
        asset_path = tmp_path / "durable.png"
        asset_path.write_bytes(b"durable image bytes")
        asset = Asset(
            "stable-asset",
            sha256(asset_path.read_bytes()).hexdigest(),
            MediaRef(path=asset_path, media_type="image/png"),
        )
        units, reps, manifest = write_batch(index_scope, asset=asset)
        other_units, other_reps, other_manifest = write_batch(other_scope)
        client = engine()
        store = AsyncPostgresDocStore(index_scope=index_scope, engine=client)
        other = AsyncPostgresDocStore(index_scope=other_scope, engine=client)
        try:
            await store.prepare()
            await store.prepare()
            async with client.connect() as connection:
                version = await connection.scalar(select(func.version()))
                assert isinstance(version, str) and version.startswith("PostgreSQL 17.")
            await store.upsert_units(units, representations=reps, manifest=manifest)
            await other.upsert_units(
                other_units, representations=other_reps, manifest=other_manifest
            )
            confirmation = VectorConfirmation(
                manifest.record_ids,
                accepted=True,
                unknown_ids=manifest.record_ids,
                guarantee="batch-timeout",
            )
            await store.record_vector_confirmation(manifest.document_id, confirmation)
            await client.dispose()
            client = engine()
            store = AsyncPostgresDocStore(index_scope=index_scope, engine=client)
            other = AsyncPostgresDocStore(index_scope=other_scope, engine=client)
            restored = await store.get_units(
                [units[1].unit_id, "missing", units[0].unit_id]
            )
            assert restored == {unit.unit_id: unit for unit in units}
            assert restored[units[0].unit_id].blocks == units[0].blocks
            assert restored[units[0].unit_id].assets == (asset,)
            actual = await store.get_representations(
                ["missing", reps[0].representation_id]
            )
            assert actual == {reps[0].representation_id: reps[0]}
            assert actual[reps[0].representation_id].content == (
                TextPart(" Actual retrieval input \n"),
            )
            assert await store.get_manifest(
                manifest.document_id
            ) == manifest.with_confirmation(confirmation)
            assert await other.get_units(manifest.unit_ids) == {}
            assert await other.get_representations(manifest.representation_ids) == {}
            await store.upsert_units(units, representations=reps, manifest=manifest)
            retried = await store.get_manifest(manifest.document_id)
            assert retried is not None and retried.confirmations == (confirmation,)
            for document, profile in (
                ("changed-document", "profile-1"),
                ("document-1", "changed-profile"),
            ):
                changed_units, changed_reps, changed_manifest = write_batch(
                    index_scope, document=document, profile=profile
                )
                with pytest.raises(ValueError, match="immutable manifest"):
                    await store.upsert_units(
                        changed_units,
                        representations=changed_reps,
                        manifest=changed_manifest,
                    )
                assert (
                    await store.get_units([unit.unit_id for unit in changed_units])
                    == {}
                )
            changed_units = (replace(units[0], metadata={"changed": True}), units[1])
            with pytest.raises(ValueError, match="immutable manifest"):
                await store.upsert_units(
                    changed_units,
                    representations=reps,
                    manifest=with_content(changed_units, reps, manifest),
                )
            omitted = with_content(
                (units[0],),
                (),
                replace(manifest, record_fingerprints={}, record_vectors={}),
            )
            with pytest.raises(ValueError, match="immutable manifest"):
                await store.upsert_units(
                    (units[0],), representations=(), manifest=omitted
                )
            with pytest.raises(ValueError, match="expected records"):
                await store.record_vector_confirmation(
                    manifest.document_id, VectorConfirmation(("unrelated",))
                )
            with pytest.raises(ValueError, match="content remains"):
                await store.delete_manifest(manifest.document_id)
            with pytest.raises(ValueError, match="surviving"):
                await store.delete_units([units[0].unit_id])
            assert await store.get_units(manifest.unit_ids) == restored
            await store.delete_units([units[1].unit_id, "absent"])
            assert await store.get_representations(manifest.representation_ids) == {}
            assert await store.get_units(manifest.unit_ids) == {
                units[0].unit_id: units[0]
            }
            with pytest.raises(ValueError, match="incomplete"):
                await store.upsert_units(units, representations=reps, manifest=manifest)
            assert await store.get_manifest(
                manifest.document_id
            ) == manifest.with_confirmation(confirmation)
            await store.delete_units([units[0].unit_id])
            await store.delete_manifest(manifest.document_id)
            await store.delete_manifest(manifest.document_id)
            assert await store.get_manifest(manifest.document_id) is None
            assert await other.get_units(other_manifest.unit_ids) == {
                unit.unit_id: unit for unit in other_units
            }
            assert asset_path.read_bytes() == b"durable image bytes"
        finally:
            await store.delete_units(manifest.unit_ids)
            await store.delete_manifest(manifest.document_id)
            await other.delete_units(other_manifest.unit_ids)
            await other.delete_manifest(other_manifest.document_id)
            await client.dispose()

    asyncio.run(run())


def test_postgres_large_id_sets_retry_and_delete_without_parameter_overflow() -> None:
    async def run() -> None:
        client = engine()
        store = AsyncPostgresDocStore(index_scope=scope(), engine=client)
        base_units, base_reps, base_manifest = write_batch(store.index_scope)
        parent = base_units[0]
        children = tuple(replace(base_units[1], ordinal=i) for i in range(1, 16384))
        units = (parent, *children)
        reps = tuple(replace(base_reps[0], unit_id=unit.unit_id) for unit in children)
        records = tuple(
            VectorRecord(
                rep.representation_id,
                rep.unit_id,
                rep.source_id,
                rep.document_id,
                rep.index_scope,
                rep.index_profile_id,
                base_manifest.space_id,
                (1.0, 0.0),
                text_view(rep.content),
                metadata=unit.metadata,
            )
            for unit, rep in zip(children, reps, strict=True)
        )
        manifest = with_content(
            units,
            reps,
            replace(
                base_manifest,
                record_fingerprints={r.record_id: r.fingerprint for r in records},
                record_vectors={r.record_id: r.vector for r in records},
            ),
        )
        try:
            await store.prepare()
            await store.upsert_units(units, representations=reps, manifest=manifest)
            missing = tuple(f"absent-{i}" for i in range(32768))
            assert len(await store.get_units((*manifest.unit_ids, *missing))) == 16384
            assert (
                len(
                    await store.get_representations(
                        (*manifest.representation_ids, *missing)
                    )
                )
                == 16383
            )
            await store.upsert_units(units, representations=reps, manifest=manifest)
            with pytest.raises(ValueError, match="surviving"):
                await store.delete_units((parent.unit_id, *missing))
            await store.delete_units(manifest.unit_ids)
            await store.delete_manifest(manifest.document_id)
            assert await store.get_units(manifest.unit_ids) == {}
            assert await store.get_representations(manifest.representation_ids) == {}
            assert await store.get_manifest(manifest.document_id) is None
        finally:
            await client.dispose()

    asyncio.run(run())


def test_postgres_parent_context_expansion_preserves_search_filters() -> None:
    from comet_rag.vectorstores.filters import Eq
    from tests.unit.test_search_context_filters import search_fixture

    async def run() -> None:
        client = engine()
        store = AsyncPostgresDocStore(index_scope=scope(), engine=client)
        try:
            await store.prepare()
            search, _, child = await search_fixture(docstore=store)
            plain = await search.run(
                "cat", mode="keyword", filters=Eq("visibility", "public")
            )
            assert plain.contexts == (child,)
            with pytest.raises(ValueError, match="context.*filter"):
                await search.run(
                    "cat",
                    mode="keyword",
                    filters=Eq("visibility", "public"),
                    expand_parents=True,
                )
        finally:
            manifests = [manifest async for manifest in store.iter_manifests()]
            for manifest in manifests:
                await store.delete_units(manifest.unit_ids)
                await store.delete_manifest(manifest.document_id)
            await client.dispose()

    asyncio.run(run())


def test_postgres_preserves_negative_zero_in_vectors_and_metadata() -> None:
    async def run() -> None:
        client = engine()
        store = AsyncPostgresDocStore(index_scope=scope(), engine=client)
        units, reps, manifest = write_batch(store.index_scope, metadata={"zero": -0.0})
        record = replace(manifest.replay_records(units, reps)[0], vector=(1.0, -0.0))
        expected = replace(
            manifest,
            record_fingerprints={record.record_id: record.fingerprint},
            record_vectors={record.record_id: record.vector},
        )
        try:
            await store.prepare()
            await store.upsert_units(units, representations=reps, manifest=expected)
            await client.dispose()
            client = engine()
            store = AsyncPostgresDocStore(
                index_scope=expected.index_scope, engine=client
            )
            actual = await store.get_manifest(expected.document_id)
            assert actual is not None and actual.manifest_id == expected.manifest_id
            restored_units = await store.get_units(actual.unit_ids)
            restored_reps = await store.get_representations(actual.representation_ids)
            replay = actual.replay_records(
                tuple(restored_units[key] for key in actual.unit_ids),
                tuple(restored_reps[key] for key in actual.representation_ids),
            )
            assert replay[0].fingerprint == record.fingerprint
            assert str(replay[0].vector[1]) == "-0.0"
            assert str(restored_units[units[0].unit_id].metadata["zero"]) == "-0.0"
            await store.upsert_units(units, representations=reps, manifest=expected)
        finally:
            await store.delete_units(expected.unit_ids)
            await store.delete_manifest(expected.document_id)
            await client.dispose()

    asyncio.run(run())


def test_postgres_final_manifest_constraint_failure_rolls_back_entire_transaction() -> (
    None
):
    async def run() -> None:
        client = engine()
        store = AsyncPostgresDocStore(index_scope=scope(), engine=client)
        units, reps, manifest = write_batch(store.index_scope)
        statements: list[str] = []

        def invalid_final_manifest(
            connection, cursor, statement, parameters, context, executemany
        ):
            statements.append(statement)
            if (
                context.compiled is not None
                and context.compiled.statement.is_insert
                and context.compiled.statement.table.name == "docstore_manifests"
            ):
                parameters = list(parameters)
                parameters[context.compiled.positiontup.index("manifest_id")] = None
                return statement, tuple(parameters)
            return statement, parameters

        try:
            await store.prepare()
            event.listen(
                client.sync_engine,
                "before_cursor_execute",
                invalid_final_manifest,
                retval=True,
            )
            try:
                with pytest.raises(IntegrityError):
                    await store.upsert_units(
                        units, representations=reps, manifest=manifest
                    )
            finally:
                event.remove(
                    client.sync_engine, "before_cursor_execute", invalid_final_manifest
                )
            assert any(
                "INSERT INTO comet_docstore_v2.docstore_units" in sql
                for sql in statements
            )
            assert any(
                "INSERT INTO comet_docstore_v2.docstore_representations" in sql
                for sql in statements
            )
            assert any(
                "INSERT INTO comet_docstore_v2.docstore_manifests" in sql
                for sql in statements
            )
            assert await store.get_units(manifest.unit_ids) == {}
            assert await store.get_representations(manifest.representation_ids) == {}
            assert await store.get_manifest(manifest.document_id) is None
            await store.upsert_units(units, representations=reps, manifest=manifest)
            assert len(await store.get_units(manifest.unit_ids)) == 2
        finally:
            await store.delete_units(manifest.unit_ids)
            await store.delete_manifest(manifest.document_id)
            await client.dispose()

    asyncio.run(run())


def test_postgres_source_lock_serializes_conflicting_batches_and_manifest_pages() -> (
    None
):
    async def run() -> None:
        client = engine()
        store = AsyncPostgresDocStore(index_scope=scope(), engine=client)
        first = write_batch(store.index_scope)
        second = write_batch(store.index_scope, document="new-document")
        manifests = [first[2], second[2]]
        try:
            await store.prepare()
            results = await asyncio.gather(
                *(
                    store.upsert_units(units, representations=reps, manifest=manifest)
                    for units, reps, manifest in (first, second)
                ),
                return_exceptions=True,
            )
            assert sum(value is None for value in results) == 1
            assert sum(isinstance(value, ValueError) for value in results) == 1
            persisted = [item async for item in store.iter_manifests(batch_size=1)]
            assert len(persisted) == 1
            winner = persisted[0]
            assert winner in manifests
            for _units, _, manifest in (first, second):
                assert len(await store.get_units(manifest.unit_ids)) == (
                    2 if manifest == winner else 0
                )
            for document in ("doc-c", "doc-a", "doc-b"):
                units, reps, manifest = write_batch(
                    store.index_scope, document=document, source=document
                )
                manifests.append(manifest)
                await store.upsert_units(units, representations=reps, manifest=manifest)
            listed = [
                item.document_id async for item in store.iter_manifests(batch_size=1)
            ]
            assert listed == sorted([winner.document_id, "doc-a", "doc-b", "doc-c"])
            assert [
                item.document_id
                async for item in store.iter_manifests(
                    document_id="doc-b", batch_size=1
                )
            ] == ["doc-b"]
            assert [
                item async for item in store.iter_manifests(document_id="absent")
            ] == []
            with pytest.raises(ValueError, match="batch_size"):
                _ = [item async for item in store.iter_manifests(batch_size=0)]
            confirmations = tuple(
                VectorConfirmation(winner.record_ids, guarantee=f"batch-{i}")
                for i in range(3)
            )
            await asyncio.gather(
                *(
                    store.record_vector_confirmation(winner.document_id, confirmation)
                    for confirmation in confirmations
                )
            )
            saved = await store.get_manifest(winner.document_id)
            assert saved is not None and set(saved.confirmations) == set(confirmations)
        finally:
            for manifest in manifests:
                await store.delete_units(manifest.unit_ids)
                await store.delete_manifest(manifest.document_id)
            await client.dispose()

    asyncio.run(run())


def test_postgres_rejects_corrupt_snapshot_identity_on_read() -> None:
    async def run() -> None:
        client = engine()
        store = AsyncPostgresDocStore(index_scope=scope(), engine=client)
        units, reps, manifest = write_batch(store.index_scope)
        try:
            await store.prepare()
            await store.upsert_units(units, representations=reps, manifest=manifest)
            for corruption in ("blocks", "metadata", "representation"):
                if corruption == "blocks":
                    block = replace(
                        units[0].blocks[1],
                        rich_text=RichText((TextRun("corrupt cell"),)),
                    )
                    changed = replace(units[0], blocks=(units[0].blocks[0], block))
                else:
                    changed = replace(units[0], metadata={"corrupt": True})
                if corruption == "representation":
                    table = store._representations
                    key = table.c.representation_id == reps[0].representation_id
                    original = _content_payload(reps[0])
                    changed_rep = replace(
                        reps[0],
                        assets=(Asset("extra", "a" * 64, MediaRef(path="stable.png")),),
                    )
                    assert changed_rep.representation_id == reps[0].representation_id
                    corrupted = {
                        **_content_payload(changed_rep),
                        "snapshot_fingerprint": original["snapshot_fingerprint"],
                    }
                else:
                    assert changed.unit_id == units[0].unit_id
                    table = store._units
                    key = table.c.unit_id == units[0].unit_id
                    original = _content_payload(units[0])
                    corrupted = {
                        **_content_payload(changed),
                        "snapshot_fingerprint": original["snapshot_fingerprint"],
                    }
                async with client.begin() as connection:
                    await connection.execute(
                        update(table)
                        .where(table.c.index_scope == store.index_scope, key)
                        .values(payload=corrupted)
                    )
                try:
                    if corruption != "representation":
                        with pytest.raises(ValueError, match="snapshot fingerprint"):
                            await store.get_units([units[1].unit_id])
                    with pytest.raises(
                        ValueError, match="snapshot fingerprint|identity"
                    ):
                        await store.get_representations(manifest.representation_ids)
                finally:
                    async with client.begin() as connection:
                        await connection.execute(
                            update(table)
                            .where(table.c.index_scope == store.index_scope, key)
                            .values(payload=original)
                        )
            wrong = replace(units[0], index_scope="wrong-scope")
            async with client.begin() as connection:
                await connection.execute(
                    update(store._units)
                    .where(
                        store._units.c.index_scope == store.index_scope,
                        store._units.c.unit_id == units[0].unit_id,
                    )
                    .values(payload=_content_payload(wrong))
                )
            with pytest.raises(ValueError, match="identity mismatch"):
                await store.get_units([units[1].unit_id])
            with pytest.raises(ValueError, match="identity mismatch"):
                await store.get_representations(manifest.representation_ids)
            async with client.begin() as connection:
                await connection.execute(
                    update(store._units)
                    .where(
                        store._units.c.index_scope == store.index_scope,
                        store._units.c.unit_id == units[0].unit_id,
                    )
                    .values(payload=_content_payload(units[0]))
                )
                corrupted = manifest_to_dict(manifest)
                corrupted["schema_version"] = 1
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(
                            update(store._manifests)
                            .where(
                                store._manifests.c.index_scope == store.index_scope,
                            )
                            .values(payload=corrupted)
                        )
            assert await store.get_manifest(manifest.document_id) == manifest
        finally:
            async with client.begin() as connection:
                await connection.execute(
                    update(store._units)
                    .where(
                        store._units.c.index_scope == store.index_scope,
                        store._units.c.unit_id == units[0].unit_id,
                    )
                    .values(payload=_content_payload(units[0]))
                )
            await store.delete_units(manifest.unit_ids)
            await store.delete_manifest(manifest.document_id)
            await client.dispose()

    asyncio.run(run())


def test_postgres_schema_identifier_is_validated_and_quoted_without_connection() -> (
    None
):
    from sqlalchemy.dialects.postgresql import dialect
    from sqlalchemy.schema import CreateSchema

    async def run() -> None:
        client = engine()
        try:
            for schema in (
                "",
                "bad.schema",
                'bad"name',
                "x; DROP SCHEMA public",
                "a" * 64,
                "中文",
            ):
                with pytest.raises(ValueError, match="schema"):
                    AsyncPostgresDocStore(
                        index_scope=scope(), engine=client, schema=schema
                    )
            store = AsyncPostgresDocStore(
                index_scope=scope(), engine=client, schema="Select"
            )
            assert (
                str(
                    CreateSchema(str(store._metadata.schema)).compile(dialect=dialect())
                )
                == 'CREATE SCHEMA "Select"'
            )
            with pytest.raises(ValueError, match="index_scope"):
                AsyncPostgresDocStore(index_scope=" ", engine=client)
        finally:
            await client.dispose()

    asyncio.run(run())
