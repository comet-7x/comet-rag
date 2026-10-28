from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from comet_rag._async import run_sync
from comet_rag.embedder.types import EmbeddingResult, EmbeddingSpace
from comet_rag.vectorstores import And, Eq, In, VectorRecord

pytestmark = pytest.mark.integration


def test_milvus_scoped_dense_bm25_filters_restart_and_precise_cleanup() -> None:
    async def scenario() -> None:
        from pymilvus import MilvusClient

        from comet_rag.vectorstores.milvus import AsyncMilvusVectorStore

        endpoint = os.environ.get("COMET_MILVUS_URI", "http://127.0.0.1:29530")
        client = await run_sync(MilvusClient, uri=endpoint, timeout=30)
        scope = f"v2it_{uuid4().hex}"
        space = EmbeddingSpace(model="integration", dimension=2, deployment="local")
        field = 'odd"\\key'
        literal = 'quoted"\\ or true or source_id == "other'
        fields = {"category": str, "number": int, "active": bool, field: str}
        store = AsyncMilvusVectorStore(
            client=client,
            index_scope=scope,
            index_profile_id="integration-v1",
            space=space,
            field_types=fields,
            timeout=45,
        )
        records = [
            VectorRecord(
                representation_id="excluded",
                unit_id="unit-excluded",
                source_id="same-source",
                document_id="snapshot-1",
                index_scope=scope,
                index_profile_id="integration-v1",
                space_id=space.space_id,
                vector=(1.0, 0.0),
                keyword_text="comet comet star",
                metadata={
                    "category": "excluded",
                    "number": 1,
                    "active": False,
                    field: "ordinary",
                },
            ),
            VectorRecord(
                representation_id="included",
                unit_id="unit-included",
                source_id="same-source",
                document_id="snapshot-1",
                index_scope=scope,
                index_profile_id="integration-v1",
                space_id=space.space_id,
                vector=(0.0, 1.0),
                keyword_text="comet star",
                metadata={
                    "category": "included",
                    "number": 2,
                    "active": True,
                    field: literal,
                },
            ),
            VectorRecord(
                representation_id="image-empty-text",
                unit_id="unit-image",
                source_id="same-source",
                document_id="snapshot-1",
                index_scope=scope,
                index_profile_id="integration-v1",
                space_id=space.space_id,
                vector=(-1.0, 0.0),
                keyword_text="",
                metadata={"category": "included", "number": 3, "active": True},
            ),
        ]
        ids = tuple(record.record_id for record in records)
        fingerprints = {record.record_id: record.fingerprint for record in records}
        query = EmbeddingResult("query", (1.0, 0.0), space.space_id)
        try:
            await store.prepare()
            confirmation = await store.upsert(records)
            assert confirmation.record_ids == ids
            assert (
                confirmation.accepted
                and confirmation.durable
                and confirmation.query_visible
            )
            assert not confirmation.unknown_ids
            assert (await store.upsert(records)).query_visible
            dense = await store.search(query)
            assert tuple(hit.record_id for hit in dense) == ids
            assert tuple(hit.score for hit in dense) == pytest.approx((1.0, 0.0, -1.0))
            assert all(hit.metric == "COSINE" and hit.higher_is_better for hit in dense)
            text = await store.search_text("comet")
            assert {hit.record_id for hit in text} == set(ids[:2])
            assert all(
                hit.metric == "BM25" and hit.score > 0 and hit.higher_is_better
                for hit in text
            )
            assert await store.search_text("") == ()
            allowed = And(
                [
                    Eq("source_id", "same-source"),
                    Eq("category", "included"),
                    In("number", [2]),
                ]
            )
            assert tuple(
                hit.record_id
                for hit in await store.search(query, limit=1, filters=allowed)
            ) == (ids[1],)
            assert tuple(
                hit.record_id
                for hit in await store.search_text("comet", limit=1, filters=allowed)
            ) == (ids[1],)
            for filters, expected in (
                (Eq(field, literal), {ids[1]}),
                (Eq("active", True), set(ids[1:])),
                (Eq("document_id", "snapshot-1"), set(ids)),
                (Eq("unit_id", "unit-image"), {ids[2]}),
                (In("category", []), set()),
                (And([Eq("category", "included"), In("number", [2, 3])]), set(ids[1:])),
            ):
                assert {
                    hit.record_id for hit in await store.search(query, filters=filters)
                } == expected
                assert {
                    hit.record_id
                    for hit in await store.search_text("comet", filters=filters)
                } == expected & set(ids[:2])
            for filters in (
                And([In("category", []), Eq("unknown", "x")]),
                Eq("number", True),
            ):
                with pytest.raises(ValueError):
                    await store.search(query, filters=filters)
                with pytest.raises(ValueError):
                    await store.search_text("", filters=filters)
            with pytest.raises(ValueError):
                await store.upsert(
                    [replace(records[0], keyword_text="different immutable content")]
                )
            assert (
                await store.verify_records(ids, fingerprints=fingerprints)
            ).present == ids
            mismatch = await store.verify_records(
                ids, fingerprints=fingerprints | {ids[0]: records[1].fingerprint}
            )
            assert mismatch.unknown == ids[:1]
            assert mismatch.present == ids[1:]
            await run_sync(client.close)
            client = await run_sync(MilvusClient, uri=endpoint, timeout=30)
            store = AsyncMilvusVectorStore(
                client=client,
                index_scope=scope,
                index_profile_id="integration-v1",
                space=space,
                field_types=fields,
                timeout=45,
            )
            await store.prepare()
            assert (
                await store.verify_records(ids, fingerprints=fingerprints)
            ).present == ids
            assert (await store.upsert(records)).query_visible
            missing_id = replace(
                records[0], representation_id="never-written"
            ).record_id
            deleted = await store.delete([ids[0], missing_id])
            assert deleted.accepted and deleted.durable and deleted.query_visible
            verification = await store.verify_records([*ids, missing_id])
            assert verification.present == ids[1:]
            assert verification.missing == (ids[0], missing_id)
            assert {hit.record_id for hit in await store.search(query)} == set(ids[1:])
            assert tuple(hit.record_id for hit in await store.search_text("comet")) == (
                ids[1],
            )
        finally:
            try:
                await store.prepare()
                await store.delete(ids)
            finally:
                await run_sync(client.close)

    asyncio.run(scenario())


def test_milvus_incompatible_prepare_preserves_collection_and_data() -> None:
    async def scenario() -> None:
        from pymilvus import MilvusClient

        from comet_rag.vectorstores.milvus import AsyncMilvusVectorStore

        client = await run_sync(
            MilvusClient,
            uri=os.environ.get("COMET_MILVUS_URI", "http://127.0.0.1:29530"),
            timeout=30,
        )
        scope = f"v2it_{uuid4().hex}"
        space = EmbeddingSpace(model="integration", dimension=2, deployment="local")
        store = AsyncMilvusVectorStore(
            client=client,
            index_scope=scope,
            index_profile_id="profile",
            space=space,
            timeout=45,
        )
        record = VectorRecord(
            representation_id="immutable",
            unit_id="unit",
            source_id="source",
            document_id="document",
            index_scope=scope,
            index_profile_id="profile",
            space_id=space.space_id,
            vector=(1.0, 0.0),
            keyword_text="comet",
        )
        try:
            await store.prepare()
            await store.upsert([record])
            original = await run_sync(client.describe_collection, store.collection_name)
            configurations = [
                {"index_profile_id": "different", "space": space},
                {
                    "index_profile_id": "profile",
                    "space": replace(space, deployment="different"),
                },
                {"index_profile_id": "profile", "space": replace(space, dimension=3)},
                {"index_profile_id": "profile", "space": replace(space, metric="IP")},
                {
                    "index_profile_id": "profile",
                    "space": space,
                    "field_types": {"category": str},
                },
            ]
            for configuration in configurations:
                incompatible = AsyncMilvusVectorStore(
                    client=client, index_scope=scope, timeout=45, **configuration
                )
                assert incompatible.collection_name == store.collection_name
                with pytest.raises(ValueError, match="incompatible"):
                    await incompatible.prepare()
                assert (
                    await run_sync(client.describe_collection, store.collection_name)
                    == original
                )
                assert (
                    await store.verify_records(
                        [record.record_id],
                        fingerprints={record.record_id: record.fingerprint},
                    )
                ).present == (record.record_id,)
            other = AsyncMilvusVectorStore(
                client=client,
                index_scope=f"v2it_{uuid4().hex}",
                index_profile_id="profile",
                space=space,
                timeout=45,
            )
            await other.prepare()
            assert other.collection_name != store.collection_name
            assert (await other.verify_records([record.record_id])).missing == (
                record.record_id,
            )
        finally:
            try:
                await store.prepare()
                await store.delete([record.record_id])
            finally:
                await run_sync(client.close)

    asyncio.run(scenario())
