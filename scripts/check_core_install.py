from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.metadata import distribution
from pathlib import Path

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.content import text_view
from comet_rag.docstores import AsyncMemoryDocStore, WriteManifest
from comet_rag.docstores.types import content_digest
from comet_rag.documents.codec import dumps, loads
from comet_rag.documents.exporters import export_markdown
from comet_rag.documents.parsers import AsyncTextParser
from comet_rag.documents.schemas import ParseResult
from comet_rag.embedder import EmbeddingSpace
from comet_rag.indexing import FlatIndexBuilder, IndexUnit, Representation
from comet_rag.indexing.codec import from_dict, to_dict
from comet_rag.pipelines import AsyncSearchPipeline
from comet_rag.sources import AsyncLocalSourceResolver, LocalSource
from comet_rag.vectorstores import AsyncMemoryVectorStore, Eq, VectorRecord

TEXT = "  comet 星系\r\n\r\n保留 Unicode 与尾部空白。  "


async def run(directory: Path, phase: str) -> None:
    state = directory / "core-check.json"
    if phase == "write":
        directory.mkdir(parents=True, exist_ok=False)
        source = directory / "source.txt"
        source.write_bytes(TEXT.encode("utf-8"))
        async with AsyncLocalSourceResolver().resolve(
            LocalSource(source, source_id="core-install")
        ) as resource:
            parsed = await AsyncTextParser().parse(
                resource, output_dir=directory / "assets"
            )
        space = EmbeddingSpace("core-check", 2, "explicit-test-vector")
        builder = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(100)))
        built = builder.build(
            parsed.document,
            index_scope="core-install",
            index_profile_id=builder.profile_id(space),
        )
        state.write_text(
            json.dumps(
                {
                    "document": dumps(parsed.document),
                    "document_id": parsed.document.document_id,
                    "markdown": export_markdown(parsed.document),
                    "units": [to_dict(unit) for unit in built.units],
                    "representations": [to_dict(rep) for rep in built.representations],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    saved = json.loads(state.read_text(encoding="utf-8"))
    document = loads(saved["document"])
    if isinstance(document, ParseResult):
        raise RuntimeError("expected a document snapshot, not a parse report")
    if document.document_id != saved["document_id"]:
        raise RuntimeError("document identity changed across processes")
    if export_markdown(document) != saved["markdown"]:
        raise RuntimeError("derived Markdown changed across processes")
    units = tuple(from_dict(value) for value in saved["units"])
    reps = tuple(from_dict(value) for value in saved["representations"])
    if not all(isinstance(unit, IndexUnit) for unit in units) or not all(
        isinstance(rep, Representation) for rep in reps
    ):
        raise RuntimeError("persisted core types differ")
    typed_units = tuple(unit for unit in units if isinstance(unit, IndexUnit))
    typed_reps = tuple(rep for rep in reps if isinstance(rep, Representation))
    if len(typed_units) != 1 or text_view(typed_units[0].content) != TEXT:
        raise RuntimeError("text was changed during parsing or codec round trip")
    space = EmbeddingSpace("core-check", 2, "explicit-test-vector")
    unit = typed_units[0]
    rep = typed_reps[0]
    # 手工向量只验证核心存储与关键词编排；真实模型验收在持久化示例。
    record = VectorRecord(
        rep.representation_id,
        unit.unit_id,
        unit.source_id,
        unit.document_id,
        unit.index_scope,
        unit.index_profile_id,
        space.space_id,
        (1.0, 0.0),
        TEXT,
    )
    manifest = WriteManifest(
        unit.index_scope,
        unit.index_profile_id,
        unit.source_id,
        unit.document_id,
        space.space_id,
        tuple(item.unit_id for item in typed_units),
        tuple(item.representation_id for item in typed_reps),
        {record.record_id: record.fingerprint},
        content_digest(typed_units, typed_reps),
        {record.record_id: record.vector},
    )
    docs = AsyncMemoryDocStore(index_scope=unit.index_scope)
    vectors = AsyncMemoryVectorStore(
        index_scope=unit.index_scope,
        index_profile_id=unit.index_profile_id,
        space=space,
    )
    await vectors.prepare()
    await docs.upsert_units(typed_units, representations=typed_reps, manifest=manifest)
    await vectors.upsert((record,))
    found = await AsyncSearchPipeline(docstore=docs, vectorstore=vectors).run(
        "comet", mode="keyword", filters=Eq("source_id", "core-install")
    )
    if not found.matched_units or found.matched_units[0].unit != unit:
        raise RuntimeError("core keyword pipeline did not return persisted content")
    forbidden = {
        "pytest",
        "docx",
        "httpx",
        "PIL",
        "torch",
        "transformers",
        "boto3",
        "asyncpg",
        "sqlalchemy",
        "pymilvus",
        "fastapi",
        "redis",
        "arq",
    }
    if forbidden.intersection(sys.modules) or distribution("comet-rag").entry_points:
        raise RuntimeError(
            "core import loaded an optional runtime or service entry point"
        )
    print(
        json.dumps(
            {
                "phase": phase,
                "document_id": document.document_id,
                "unit_id": unit.unit_id,
                "sdk_imports": [],
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run outside the checkout with only the core wheel installed"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("write", "read"), required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output_dir, args.phase))


if __name__ == "__main__":
    main()
