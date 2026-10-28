from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import create_async_engine

from comet_rag._async import run_sync
from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.content import text_view
from comet_rag.docstores.postgres import AsyncPostgresDocStore
from comet_rag.documents.parsers import AsyncDocxParser, AsyncTextParser
from comet_rag.embedder.models.clip import AsyncCLIPEmbedder
from comet_rag.identity import stable_id
from comet_rag.indexing.builders.flat import FlatIndexBuilder
from comet_rag.pipelines import AsyncIngestionPipeline, AsyncSearchPipeline
from comet_rag.sources import AsyncLocalSourceResolver, LocalSource
from comet_rag.vectorstores import Eq
from comet_rag.vectorstores.milvus import AsyncMilvusVectorStore

MODEL = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
REVISION = "1a25a446712ba5ee05982a381eed697ef9b435cf"  # pragma: allowlist secret
TEXTS = {
    "cat": "A cat resting indoors. Cats purr softly and have whiskers.",
    "dog": "A dog playing outdoors. Dogs run and fetch a ball.",
    "space": "Saturn is a planet with rings. The telescope observes distant stars.",
}


async def run(args: argparse.Namespace) -> None:
    model = await AsyncCLIPEmbedder.load(
        args.model_dir, model_id=MODEL, revision=REVISION
    )
    builder = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(180)))
    engine = create_async_engine(os.environ["COMET_RAG_TEST_POSTGRES_URL"])
    client = await run_sync(MilvusClient, uri=args.milvus_uri, timeout=45)
    docs = AsyncPostgresDocStore(index_scope=args.scope, engine=engine)
    vectors = AsyncMilvusVectorStore(
        client=client,
        index_scope=args.scope,
        index_profile_id=builder.profile_id(model.space),
        space=model.space,
        timeout=120,
    )
    root = args.output_dir.resolve()
    state_path = root / "manifest-check.json"
    try:
        await docs.prepare()
        await vectors.prepare()
        if args.phase == "ingest":
            root.mkdir(parents=True, exist_ok=False)
            inputs = root / "inputs"
            inputs.mkdir()
            pipeline = AsyncIngestionPipeline(
                resolver=AsyncLocalSourceResolver(),
                parser=AsyncTextParser(),
                builder=builder,
                embedder=model,
                docstore=docs,
                vectorstore=vectors,
                output_dir=root / "assets",
            )
            expected = {}
            for source_id, text in TEXTS.items():
                path = inputs / f"{source_id}.txt"
                path.write_bytes(text.encode())
                result = await pipeline.run(LocalSource(path, source_id=source_id))
                if not result.database_committed or not result.query_visible:
                    raise RuntimeError("ingestion did not confirm complete visibility")
                expected[source_id] = result.document_id
                retry = await pipeline.run(LocalSource(path, source_id=source_id))
                if retry.manifest.manifest_id != result.manifest.manifest_id:
                    raise RuntimeError("retry changed immutable manifest")
            from docx import Document

            docx_path = inputs / "table.docx"
            native = Document()
            table = native.add_table(rows=2, cols=2)
            for cell, text in zip(
                (cell for row in table.rows for cell in row.cells),
                ("Material", "Density", "Iron", "7.87"),
                strict=True,
            ):
                cell.text = text
            native.save(docx_path)
            docx_pipeline = AsyncIngestionPipeline(
                resolver=AsyncLocalSourceResolver(),
                parser=AsyncDocxParser(),
                builder=builder,
                embedder=model,
                docstore=docs,
                vectorstore=vectors,
                output_dir=root / "assets",
            )
            result = await docx_pipeline.run(LocalSource(docx_path, source_id="table"))
            expected["table"] = result.document_id
            state_path.write_text(
                json.dumps(
                    {
                        "scope": args.scope,
                        "space_id": model.space.space_id,
                        "documents": expected,
                    },
                    indent=2,
                )
            )
        state = json.loads(state_path.read_text())
        if state["scope"] != args.scope or state["space_id"] != model.space.space_id:
            raise RuntimeError("saved verification scope or actual model space differs")
        for source_id, document_id in state["documents"].items():
            manifest = await docs.get_manifest(document_id)
            if manifest is None or manifest.source_id != source_id:
                raise RuntimeError("persisted document manifest missing")
            actual = await vectors.verify_records(
                manifest.record_ids, fingerprints=manifest.record_fingerprints
            )
            if (
                set(actual.present) != set(manifest.record_ids)
                or actual.missing
                or actual.unknown
            ):
                raise RuntimeError("persisted exact vector verification failed")
            units = await docs.get_units(manifest.unit_ids)
            reps = await docs.get_representations(manifest.representation_ids)
            if set(units) != set(manifest.unit_ids) or set(reps) != set(
                manifest.representation_ids
            ):
                raise RuntimeError("persisted unit or actual representation is missing")
            if source_id == "table" and not any(
                block.table is not None
                for unit in units.values()
                for block in unit.blocks
            ):
                raise RuntimeError("DOCX table structure was not retained")
        outputs = []
        for mode in ("dense", "keyword", "hybrid"):
            search = AsyncSearchPipeline(
                docstore=docs,
                vectorstore=vectors,
                embedder=None if mode == "keyword" else model,
            )
            found = await search.run("cat", mode=mode, top_k=1)
            if (
                not found.matched_units
                or found.matched_units[0].unit.source_id != "cat"
            ):
                raise RuntimeError(
                    f"{mode} did not retrieve expected cat document: "
                    f"{[(match.unit.source_id, [(ev.hit.score, ev.hit.metric) for ev in match.evidence]) for match in found.matched_units]}"
                )
            outputs.append(
                {
                    "mode": mode,
                    "source_id": found.matched_units[0].unit.source_id,
                    "content": text_view(found.matched_units[0].unit.content),
                }
            )
            filtered = await search.run(
                "cat", mode=mode, top_k=1, filters=Eq("source_id", "dog")
            )
            if any(item.unit.source_id != "dog" for item in filtered.matched_units):
                raise RuntimeError("backend filter escaped expected source")
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "scope": args.scope,
                    "space_id": model.space.space_id,
                    "documents": len(state["documents"]),
                    "search": outputs,
                },
                indent=2,
            )
        )
    finally:
        await engine.dispose()
        await run_sync(client.close)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real local CLIP, PostgreSQL and Milvus text ingestion/search"
    )
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--scope", default=stable_id("example", "persistent-text"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:29530")
    parser.add_argument("--phase", choices=("ingest", "verify"), required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
