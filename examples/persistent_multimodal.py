from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import httpx
from docx import Document
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import create_async_engine

from comet_rag._async import run_sync
from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.content import ImagePart, text_view
from comet_rag.docstores.postgres import AsyncPostgresDocStore
from comet_rag.documents.parsers import AsyncDocxParser, AsyncTextParser
from comet_rag.documents.parsers.image import AsyncImageParser
from comet_rag.embedder.models.clip import AsyncCLIPEmbedder
from comet_rag.identity import file_hash
from comet_rag.indexing.builders.flat import FlatIndexBuilder
from comet_rag.pipelines import AsyncIngestionPipeline, AsyncSearchPipeline
from comet_rag.sources import AsyncLocalSourceResolver, HttpSource, LocalSource
from comet_rag.sources.http import AsyncHttpSourceResolver
from comet_rag.vectorstores import Eq, In
from comet_rag.vectorstores.milvus import AsyncMilvusVectorStore

from .persistent_text import MODEL, REVISION

DESCRIPTIONS = {
    "cat": "Two tabby cats lying on a pink couch next to remote controls.",
    "dog": "A fluffy white dog sitting on grass outdoors.",
}


class _ImageHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _serve_images(directory: Path) -> Iterator[str]:
    handler = partial(_ImageHandler, directory=str(directory.resolve()))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


async def run(args: argparse.Namespace, *, image_url: str | None) -> None:
    started = perf_counter()
    model = await AsyncCLIPEmbedder.load(
        args.model_dir, model_id=MODEL, revision=REVISION
    )
    builder = FlatIndexBuilder(
        DocumentChunker(RecursiveTextChunker(180)), representation="separate"
    )
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
            downloads = root / "downloads"
            downloads.mkdir()
            documents = {}
            text_pipeline = AsyncIngestionPipeline(
                resolver=AsyncLocalSourceResolver(),
                parser=AsyncTextParser(),
                builder=builder,
                embedder=model,
                docstore=docs,
                vectorstore=vectors,
                output_dir=root / "assets",
            )
            for subject, text in DESCRIPTIONS.items():
                path = inputs / f"{subject}.txt"
                path.write_text(text, encoding="utf-8")
                result = await text_pipeline.run(
                    LocalSource(path, source_id=f"text-{subject}")
                )
                if not result.query_visible:
                    raise RuntimeError("text records are not query-visible")
                documents[f"text-{subject}"] = result.document_id
            if image_url is None:
                raise ValueError("ingestion requires the local image fixture server")
            async with httpx.AsyncClient(trust_env=False) as http:
                resolver = AsyncHttpSourceResolver(
                    http,
                    policy=lambda source: (
                        urlsplit(source.url).netloc == urlsplit(image_url).netloc
                    ),
                    temp_dir=downloads,
                )
                image_pipeline = AsyncIngestionPipeline(
                    resolver=resolver,
                    parser=AsyncImageParser(),
                    builder=builder,
                    embedder=model,
                    docstore=docs,
                    vectorstore=vectors,
                    output_dir=root / "assets",
                )
                for subject, filename in (("cat", "cat.png"), ("dog", "dog.jpg")):
                    result = await image_pipeline.run(
                        HttpSource(
                            f"{image_url}/{filename}", source_id=f"image-{subject}"
                        )
                    )
                    if not result.query_visible:
                        raise RuntimeError("image records are not query-visible")
                    documents[f"image-{subject}"] = result.document_id
            if any(downloads.iterdir()):
                raise RuntimeError("HTTP source temporary files were not released")
            native = Document()
            native.add_picture(str(args.image_dir / "cat.png"))
            docx_path = inputs / "embedded-image.docx"
            native.save(docx_path)
            native_pipeline = AsyncIngestionPipeline(
                resolver=AsyncLocalSourceResolver(),
                parser=AsyncDocxParser(),
                builder=builder,
                embedder=model,
                docstore=docs,
                vectorstore=vectors,
                output_dir=root / "assets",
            )
            result = await native_pipeline.run(
                LocalSource(docx_path, source_id="docx-image-cat")
            )
            if not result.query_visible:
                raise RuntimeError("embedded image records are not query-visible")
            documents["docx-image-cat"] = result.document_id
            state_path.write_text(
                json.dumps(
                    {
                        "scope": args.scope,
                        "space_id": model.space.space_id,
                        "documents": documents,
                        "images": {
                            filename: file_hash(args.image_dir / filename)
                            for filename in ("cat.png", "dog.jpg")
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["scope"] != args.scope or state["space_id"] != model.space.space_id:
            raise RuntimeError("persisted scope/model differs")
        image_inputs = {}
        for source_id, document_id in state["documents"].items():
            manifest = await docs.get_manifest(document_id)
            if manifest is None or manifest.source_id != source_id:
                raise RuntimeError("persisted manifest is missing")
            verification = await vectors.verify_records(
                manifest.record_ids, fingerprints=manifest.record_fingerprints
            )
            if (
                set(verification.present) != set(manifest.record_ids)
                or verification.missing
                or verification.unknown
            ):
                raise RuntimeError("persisted exact vector verification failed")
            units = await docs.get_units(manifest.unit_ids)
            reps = await docs.get_representations(manifest.representation_ids)
            if set(units) != set(manifest.unit_ids) or set(reps) != set(
                manifest.representation_ids
            ):
                raise RuntimeError("persisted content or actual inputs are missing")
            for unit in units.values():
                for asset in unit.assets:
                    if (
                        asset.media.path is None
                        or file_hash(asset.media.path) != asset.content_hash
                    ):
                        raise RuntimeError(
                            "persistent image bytes are missing or changed"
                        )
                if source_id.startswith("image-"):
                    image_inputs[source_id.removeprefix("image-")] = tuple(
                        part for part in unit.content if isinstance(part, ImagePart)
                    )
                    if text_view(unit.content):
                        raise RuntimeError(
                            "image indexing unexpectedly substituted text"
                        )
        search = AsyncSearchPipeline(docstore=docs, vectorstore=vectors, embedder=model)
        observations = []
        for subject, text in DESCRIPTIONS.items():
            for query_kind, query in (("text", text), ("image", image_inputs[subject])):
                for target in ("text", "image"):
                    query_started = perf_counter()
                    found = await search.run(
                        query,
                        mode="dense" if query_kind == "text" else None,
                        top_k=1,
                        filters=In("source_id", (f"{target}-cat", f"{target}-dog")),
                    )
                    expected = f"{target}-{subject}"
                    if (
                        not found.matched_units
                        or found.matched_units[0].unit.source_id != expected
                    ):
                        raise RuntimeError(
                            f"{query_kind}->{target} failed for {subject}: "
                            f"{[item.unit.source_id for item in found.matched_units]}"
                        )
                    match = found.matched_units[0]
                    observations.append(
                        {
                            "query": query_kind,
                            "target": target,
                            "subject": subject,
                            "source_id": match.unit.source_id,
                            "unit_id": match.unit.unit_id,
                            "scores": [ev.hit.score for ev in match.evidence],
                            "elapsed_seconds": perf_counter() - query_started,
                        }
                    )
        embedded = await search.run(
            DESCRIPTIONS["cat"],
            mode="dense",
            top_k=1,
            filters=Eq("source_id", "docx-image-cat"),
        )
        if not embedded.matched_units or not embedded.matched_units[0].unit.assets:
            raise RuntimeError("DOCX embedded image was not retained and retrieved")
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "scope": args.scope,
                    "space_id": model.space.space_id,
                    "documents": len(state["documents"]),
                    "images": state["images"],
                    "observations": observations,
                    "embedded_docx_image": True,
                    "http_downloads_released": not any((root / "downloads").iterdir()),
                    "elapsed_seconds": perf_counter() - started,
                },
                indent=2,
            )
        )
    finally:
        await engine.dispose()
        await run_sync(client.close)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real CLIP text/image retrieval with retained HTTP and DOCX assets"
    )
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--image-dir", type=Path)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:29530")
    parser.add_argument("--phase", choices=("ingest", "verify"), required=True)
    args = parser.parse_args()
    if args.phase == "ingest":
        if args.image_dir is None:
            parser.error(
                "--image-dir containing cat.png and dog.jpg is required for ingest"
            )
        with _serve_images(args.image_dir) as url:
            asyncio.run(run(args, image_url=url))
    else:
        asyncio.run(run(args, image_url=None))


if __name__ == "__main__":
    main()
