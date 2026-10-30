from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pymilvus import AsyncMilvusClient
from sqlalchemy.ext.asyncio import create_async_engine

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.content import text_view
from comet_rag.docstores.postgres import AsyncPostgresDocStore
from comet_rag.documents.parsers.text import AsyncTextParser
from comet_rag.errors import IngestionIncompleteError
from comet_rag.indexing import FlatIndexBuilder
from comet_rag.pipelines import (
    AsyncIngestionPipeline,
    AsyncSearchPipeline,
    delete_document,
)
from comet_rag.sources import AsyncLocalSourceResolver, LocalSource
from comet_rag.vectorstores.milvus import AsyncMilvusVectorStore
from tests.unit.test_pipelines import ProbeEmbedder

pytestmark = pytest.mark.integration


class LostResponseClient(AsyncMilvusClient):
    lose_upsert = False
    lose_delete = False

    async def upsert(
        self, collection_name, data, timeout=None, partition_name="", **kwargs
    ):
        result = await super().upsert(
            collection_name,
            data,
            timeout=timeout,
            partition_name=partition_name,
            **kwargs,
        )
        if self.lose_upsert:
            self.lose_upsert = False
            # 服务端真实落盘后丢弃响应，不能用未发请求的超时代替未知结果。
            await super().flush(collection_name, timeout=timeout)
            raise TimeoutError("injected lost upsert response after flush")
        return result

    async def delete(
        self,
        collection_name,
        ids=None,
        timeout=None,
        filter=None,
        partition_name=None,
        **kwargs,
    ):
        result = await super().delete(
            collection_name,
            ids=ids,
            timeout=timeout,
            filter=filter,
            partition_name=partition_name,
            **kwargs,
        )
        if self.lose_delete:
            self.lose_delete = False
            await super().flush(collection_name, timeout=timeout)
            raise TimeoutError("injected lost delete response after flush")
        return result


def test_rebuild_lost_responses_reconnect_replay_and_exact_delete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        endpoint = os.environ.get("COMET_MILVUS_URI", "http://127.0.0.1:29530")
        database_url = os.environ["COMET_RAG_TEST_POSTGRES_URL"]
        scopes = (f"recovery_a_{uuid4().hex}", f"recovery_b_{uuid4().hex}")
        model = ProbeEmbedder()
        builder = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(100)))
        source_path = tmp_path / "cat.txt"
        source = LocalSource(source_path, source_id="same-source")
        source_path.write_text("cat original snapshot", encoding="utf-8")

        def connect():
            engine = create_async_engine(database_url)
            client = LostResponseClient(
                uri=endpoint,
                db_name=os.environ.get("COMET_MILVUS_DB", "default"),
                timeout=30,
            )
            docs = tuple(
                AsyncPostgresDocStore(index_scope=scope, engine=engine)
                for scope in scopes
            )
            vectors = tuple(
                AsyncMilvusVectorStore(
                    client=client,
                    index_scope=scope,
                    index_profile_id=builder.profile_id(model.space),
                    space=model.space,
                    timeout=45,
                )
                for scope in scopes
            )
            pipelines = tuple(
                AsyncIngestionPipeline(
                    resolver=AsyncLocalSourceResolver(),
                    parser=AsyncTextParser(),
                    builder=builder,
                    embedder=model,
                    docstore=doc,
                    vectorstore=vector,
                    output_dir=tmp_path / "assets",
                )
                for doc, vector in zip(docs, vectors, strict=True)
            )
            return engine, client, docs, vectors, pipelines

        async def assert_original_search(doc, vector) -> None:
            search = AsyncSearchPipeline(
                docstore=doc,
                vectorstore=vector,
                embedder=model,
            )
            for mode in ("dense", "keyword", "hybrid"):
                found = await search.run("cat", mode=mode)
                assert len(found.matched_units) == 1
                assert (
                    text_view(found.matched_units[0].unit.content)
                    == "cat original snapshot"
                )

        engine, client, docs, vectors, pipelines = connect()
        manifests = {}
        try:
            for doc in docs:
                await doc.prepare()
            original = await pipelines[0].run(source)
            manifests[0] = original.manifest
            source_path.write_text("cat rebuilt snapshot", encoding="utf-8")
            client.lose_upsert = True
            with pytest.raises(IngestionIncompleteError) as caught:
                await pipelines[1].run(source)
            assert isinstance(caught.value.__cause__, TimeoutError)
            incomplete = caught.value.result
            manifests[1] = incomplete.manifest
            assert incomplete.database_committed and not incomplete.query_visible
            pending = await docs[1].get_manifest(incomplete.document_id)
            assert pending is not None
            assert pending.confirmations[-1].unknown_ids == pending.record_ids
            present = await vectors[1].verify_records(
                pending.record_ids,
                fingerprints=pending.record_fingerprints,
            )
            assert set(present.present) == set(pending.record_ids)
            assert not present.missing and not present.unknown
            assert set(original.manifest.record_ids).isdisjoint(pending.record_ids)
            await assert_original_search(docs[0], vectors[0])

            await client.close()
            await engine.dispose()
            engine, client, docs, vectors, pipelines = connect()
            for vector in vectors:
                await vector.prepare()
            restored = await docs[1].get_manifest(incomplete.document_id)
            assert restored == pending
            before = model.calls.count("document")
            recovered = await pipelines[1].run(source)
            assert recovered.query_visible
            assert model.calls.count("document") == before
            assert recovered.manifest.manifest_id == pending.manifest_id
            assert recovered.manifest.record_vectors == pending.record_vectors
            assert recovered.manifest.record_fingerprints == pending.record_fingerprints
            assert recovered.manifest.confirmations[-1].query_visible
            await assert_original_search(docs[0], vectors[0])
            switched = await AsyncSearchPipeline(
                docstore=docs[1],
                vectorstore=vectors[1],
            ).run("cat", mode="keyword")
            assert len(switched.matched_units) == 1
            assert (
                text_view(switched.matched_units[0].unit.content)
                == "cat rebuilt snapshot"
            )

            client.lose_delete = True
            with pytest.raises(TimeoutError, match="lost delete response"):
                await delete_document(
                    document_id=incomplete.document_id,
                    docstore=docs[1],
                    vectorstore=vectors[1],
                )
            assert (
                await docs[1].get_manifest(incomplete.document_id) == recovered.manifest
            )
            assert set(await docs[1].get_units(pending.unit_ids)) == set(
                pending.unit_ids
            )
            assert set(
                await docs[1].get_representations(pending.representation_ids)
            ) == set(pending.representation_ids)
            assert (
                await vectors[1].verify_records(pending.record_ids)
            ).missing == pending.record_ids

            await client.close()
            await engine.dispose()
            engine, client, docs, vectors, pipelines = connect()
            for vector in vectors:
                await vector.prepare()
            assert (
                await docs[1].get_manifest(incomplete.document_id) == recovered.manifest
            )
            await delete_document(
                document_id=incomplete.document_id,
                docstore=docs[1],
                vectorstore=vectors[1],
            )
            assert await docs[1].get_manifest(incomplete.document_id) is None
            assert await docs[1].get_units(pending.unit_ids) == {}
            assert await docs[1].get_representations(pending.representation_ids) == {}
            assert (
                await vectors[1].verify_records(pending.record_ids)
            ).missing == pending.record_ids
            assert await docs[0].get_manifest(original.document_id) == original.manifest
            await assert_original_search(docs[0], vectors[0])
            assert source_path.read_text(encoding="utf-8") == "cat rebuilt snapshot"
        finally:
            try:
                for index, manifest in manifests.items():
                    await vectors[index].prepare()
                    await delete_document(
                        document_id=manifest.document_id,
                        docstore=docs[index],
                        vectorstore=vectors[index],
                    )
            finally:
                try:
                    await client.close()
                finally:
                    await engine.dispose()

    asyncio.run(scenario())
