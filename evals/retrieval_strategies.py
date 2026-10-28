"""Small offline evaluation; run with python -m evals.retrieval_strategies --help."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Literal, cast

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.content import (
    ContentInput,
    ContentItem,
    ImagePart,
    MediaRef,
    content_parts,
    text_view,
)
from comet_rag.docstores.memory import AsyncMemoryDocStore
from comet_rag.docstores.types import WriteManifest, content_digest
from comet_rag.documents.schemas import (
    Asset,
    Block,
    ParsedDocument,
    ParsingInfo,
    RichText,
    SourceInfo,
    TextRun,
)
from comet_rag.embedder.models.clip import AsyncCLIPEmbedder
from comet_rag.identity import file_hash
from comet_rag.indexing.base import IndexBuilder
from comet_rag.indexing.builders.flat import FlatIndexBuilder
from comet_rag.indexing.builders.parent_child import ParentChildIndexBuilder
from comet_rag.indexing.types import IndexUnit, Representation, refs_value
from comet_rag.pipelines.search import AsyncSearchPipeline
from comet_rag.rerankers.models.local import AsyncLocalReranker
from comet_rag.retrieval.types import SearchResult
from comet_rag.vectorstores.memory import AsyncMemoryVectorStore
from comet_rag.vectorstores.types import VectorRecord

CLIP_ID = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
CLIP_REVISION = "1a25a446712ba5ee05982a381eed697ef9b435cf"  # pragma: allowlist secret
RERANKER_ID = "cross-encoder/ms-marco-MiniLM-L6-v2"
RERANKER_REVISION = (
    "233902d25c440f23af6f7d6e94d2946bac0bee0a"  # pragma: allowlist secret
)

# Labels are fixed independently of model scores and are never tuned by this script.
CORPUS = {
    "account_reset": "A locked account can be recovered using the password reset link sent to the registered email address. If that mailbox is unavailable, contact support to verify account ownership.",
    "flight_refund": "When the airline cancels a flight, passengers can request a refund to the original payment method. A travel voucher is optional; customers do not have to accept credit instead of money.",
    "flight_change": "To move a flight to another date, open the booking and select change departure. A fare difference may apply. Changing a ticket date does not automatically cancel the booking or request a refund.",
    "order_return": "Clothing bought online can be returned within thirty days if unworn and with tags attached. Select return item in the order page, choose the reason, and print the prepaid shipping label.",
    "battery_swelling": "A swollen phone battery is a fire hazard. Stop charging and using the device, keep it away from flammable material, and arrange professional battery disposal. Do not puncture or crush the battery.",
    "battery_cold": "Cold weather temporarily reduces lithium battery capacity. Allow a cold device to reach room temperature before charging. Reduced runtime in the cold alone does not mean the battery is swollen.",
    "database_restore": "After an accidental table deletion, restore the latest database backup into a separate instance and replay transaction logs to just before the mistake. Verify recovered rows before replacing production data.",
    "backup_retention": "Database backups are retained for thirty days and stored in a separate region. A daily backup is scheduled at midnight. Retention policy defines how long backups remain available, not how to restore them.",
    "tomato_nitrogen": "Yellowing that begins on older tomato leaves can indicate nitrogen deficiency. Check soil nutrients and apply an appropriate balanced fertilizer. Avoid assuming every yellow leaf is caused by overwatering.",
    "tomato_watering": "Tomato plants with drooping leaves and dry soil need deep watering near the roots. Water in the morning and check soil moisture first. Excess watering can damage roots even when leaves appear wilted.",
    "retirement_match": "An employer retirement match adds a company contribution when an employee saves part of their salary. Contribute enough to qualify for the full available match, subject to the plan rules and vesting schedule.",
    "retirement_withdrawal": "Taking money out of a retirement account before the permitted age may trigger income tax and an early withdrawal penalty. Some exceptions apply. Withdrawal rules differ from employer contribution matching rules.",
}


@dataclass(frozen=True, slots=True)
class Query:
    query_id: str
    text: str
    relevant_sources: tuple[str, ...]


QUERIES = (
    Query("q01", "How do I get back into my locked account?", ("account_reset",)),
    Query(
        "q02",
        "The airline canceled my flight. Can I get my money back rather than a voucher?",
        ("flight_refund",),
    ),
    Query(
        "q03",
        "How can I move my departure to a later day without canceling my trip?",
        ("flight_change",),
    ),
    Query(
        "q04",
        "My online clothing order is the wrong size. How do I send it back?",
        ("order_return",),
    ),
    Query(
        "q05",
        "My phone battery is bulging. Is it safe to keep charging it?",
        ("battery_swelling",),
    ),
    Query(
        "q06",
        "I accidentally deleted a database table. How can I recover the lost rows?",
        ("database_restore",),
    ),
    Query(
        "q07",
        "Older leaves on my tomato plant are turning yellow. Which nutrient may be missing?",
        ("tomato_nitrogen",),
    ),
    Query(
        "q08",
        "How can I receive the full company contribution to my retirement savings?",
        ("retirement_match",),
    ),
)


@dataclass(slots=True)
class LoadedIndex:
    docstore: AsyncMemoryDocStore
    vectorstore: AsyncMemoryVectorStore
    units: tuple[IndexUnit, ...]
    representations: tuple[Representation, ...]
    evidence: dict[str, object]


def text_documents() -> tuple[ParsedDocument, ...]:
    return tuple(
        ParsedDocument(
            source=SourceInfo(label, hashlib.sha256(text.encode()).hexdigest()),
            parsing=ParsingInfo("evaluation-authored-fixture", version="1"),
            blocks=(Block("body", "paragraph", rich_text=RichText((TextRun(text),))),),
        )
        for label, text in CORPUS.items()
    )


def image_documents(image_dir: Path) -> tuple[ParsedDocument, ...]:
    documents = []
    for label, name, media_type in (
        ("cat", "cat.png", "image/png"),
        ("dog", "dog.jpg", "image/jpeg"),
    ):
        path = (image_dir / name).resolve(strict=True)
        digest = file_hash(path)
        asset = Asset(
            f"image_{label}",
            digest,
            MediaRef(path=path, media_type=media_type),
            size_bytes=path.stat().st_size,
            provenance="original",
        )
        documents.append(
            ParsedDocument(
                SourceInfo(label, digest),
                ParsingInfo("evaluation-labeled-image", version="1"),
                (
                    Block(
                        "image",
                        "image",
                        asset_id=asset.asset_id,
                        alt_text=f"A photograph of a {label}.",
                    ),
                ),
                assets=(asset,),
            )
        )
    return tuple(documents)


def unit_evidence(unit: IndexUnit) -> dict[str, object]:
    text = text_view(unit.content)
    return {
        "unit_id": unit.unit_id,
        "source_id": unit.source_id,
        "parent_unit_id": unit.parent_unit_id,
        "text": text,
        "text_chars": len(text),
        "text_utf8_bytes": len(text.encode()),
        "image_parts": sum(
            isinstance(part, ImagePart) for part in content_parts(unit.content)
        ),
        "source_refs": refs_value(unit.source_refs),
    }


async def load_index(
    documents: tuple[ParsedDocument, ...],
    builder: IndexBuilder,
    embedder: AsyncCLIPEmbedder,
    name: str,
) -> LoadedIndex:
    profile = builder.profile_id(embedder.space)
    docstore = AsyncMemoryDocStore(index_scope=name)
    vectorstore = AsyncMemoryVectorStore(
        index_scope=name, index_profile_id=profile, space=embedder.space
    )
    await vectorstore.prepare()
    units: list[IndexUnit] = []
    representations: list[Representation] = []
    build_seconds = embed_seconds = store_seconds = 0.0
    for document in documents:
        started = time.perf_counter()
        built = builder.build(document, index_scope=name, index_profile_id=profile)
        build_seconds += time.perf_counter() - started
        started = time.perf_counter()
        embeddings = await embedder.embed(
            tuple(
                ContentItem(rep.representation_id, rep.content)
                for rep in built.representations
            ),
            role="document",
        )
        embed_seconds += time.perf_counter() - started
        if tuple(result.item_id for result in embeddings) != tuple(
            rep.representation_id for rep in built.representations
        ):
            raise ValueError("evaluation embedding IDs do not match representations")
        records = tuple(
            VectorRecord(
                rep.representation_id,
                rep.unit_id,
                rep.source_id,
                rep.document_id,
                rep.index_scope,
                rep.index_profile_id,
                result.space_id,
                result.vector,
                text_view(rep.content),
            )
            for rep, result in zip(built.representations, embeddings, strict=True)
        )
        manifest = WriteManifest(
            name,
            profile,
            document.source.source_id,
            document.document_id,
            embedder.space.space_id,
            tuple(unit.unit_id for unit in built.units),
            tuple(rep.representation_id for rep in built.representations),
            {record.record_id: record.fingerprint for record in records},
            content_digest(built.units, built.representations),
        )
        started = time.perf_counter()
        await docstore.upsert_units(
            built.units, representations=built.representations, manifest=manifest
        )
        confirmation = await vectorstore.upsert(records)
        await docstore.record_vector_confirmation(document.document_id, confirmation)
        verification = await vectorstore.verify_records(
            manifest.record_ids, fingerprints=manifest.record_fingerprints
        )
        if (
            set(verification.present) != set(manifest.record_ids)
            or verification.missing
            or verification.unknown
        ):
            raise ValueError("evaluation records are not all query-visible")
        store_seconds += time.perf_counter() - started
        units.extend(built.units)
        representations.extend(built.representations)
    parent_ids = {
        unit.parent_unit_id for unit in units if unit.parent_unit_id is not None
    }
    parents = [unit for unit in units if unit.unit_id in parent_ids]
    return LoadedIndex(
        docstore,
        vectorstore,
        tuple(units),
        tuple(representations),
        {
            "index_scope": name,
            "profile_id": profile,
            "unit_count": len(units),
            "representation_count": len(representations),
            "parent_count": len(parents),
            "unit_text_chars": sum(len(text_view(unit.content)) for unit in units),
            "parent_text_chars": sum(len(text_view(unit.content)) for unit in parents),
            "representation_text_chars": sum(
                len(text_view(rep.content)) for rep in representations
            ),
            "representation_image_count": sum(
                isinstance(part, ImagePart)
                for rep in representations
                for part in content_parts(rep.content)
            ),
            "representation_clip_text_tokens": sum(
                len(
                    embedder.processor.tokenizer(
                        text_view(rep.content),
                        add_special_tokens=True,
                        truncation=False,
                    )["input_ids"]
                )
                for rep in representations
                if not any(
                    isinstance(part, ImagePart) for part in content_parts(rep.content)
                )
            ),
            "dense_vector_float32_bytes": len(representations)
            * embedder.space.dimension
            * 4,
            "build_seconds": build_seconds,
            "document_embedding_seconds": embed_seconds,
            "memory_write_seconds": store_seconds,
            "units": [unit_evidence(unit) for unit in units],
            "representations": [
                {
                    "representation_id": rep.representation_id,
                    "unit_id": rep.unit_id,
                    "purpose": rep.purpose,
                    "text": text_view(rep.content),
                    "content_fingerprint": rep.content_fingerprint,
                }
                for rep in representations
            ],
        },
    )


def result_evidence(
    result: SearchResult, relevant: tuple[str, ...]
) -> dict[str, object]:
    ranking = list(
        dict.fromkeys(match.unit.source_id for match in result.matched_units)
    )
    ranks = [rank for rank, source in enumerate(ranking, 1) if source in relevant]
    return {
        "source_ranking": ranking,
        "metrics": {
            "reciprocal_rank": 1.0 / min(ranks) if ranks else 0.0,
            "hit_at_1": float(bool(set(ranking[:1]) & set(relevant))),
            "hit_at_3": float(bool(set(ranking[:3]) & set(relevant))),
            "recall_at_3": len(set(ranking[:3]) & set(relevant)) / len(relevant),
        },
        "reranked": result.reranked,
        "diagnostics": list(result.diagnostics),
        "matched_units": [
            {
                **unit_evidence(match.unit),
                "fusion_score": match.fusion_score,
                "rerank_score": match.rerank_score,
                "retrieval_evidence": [
                    {
                        "channel": item.channel,
                        "rank": item.rank,
                        "raw_score": item.hit.score,
                        "metric": item.hit.metric,
                        "higher_is_better": item.hit.higher_is_better,
                        "record_id": item.hit.record_id,
                        "representation_id": item.hit.representation_id,
                    }
                    for item in match.evidence
                ],
            }
            for match in result.matched_units
        ],
        "contexts": [unit_evidence(unit) for unit in result.contexts],
        "context_count": len(result.contexts),
        "context_text_chars": sum(
            len(text_view(unit.content)) for unit in result.contexts
        ),
        "context_utf8_bytes": sum(
            len(text_view(unit.content).encode()) for unit in result.contexts
        ),
        "rerank_result": asdict(result.rerank_result)
        if result.rerank_result is not None
        else None,
    }


def validate_fixtures() -> None:
    if len(QUERIES) < 6 or len({query.query_id for query in QUERIES}) != len(QUERIES):
        raise ValueError("evaluation requires six unique independently labeled queries")
    if any(
        not query.relevant_sources or set(query.relevant_sources) - set(CORPUS)
        for query in QUERIES
    ):
        raise ValueError("every relevance label must refer to authored corpus content")
    if any(not text or len(text) > 320 for text in CORPUS.values()):
        raise ValueError(
            "text fixtures must fit the declared whole-document parent boundary"
        )


def aggregate(rows: list[dict[str, object]]) -> dict[str, float]:
    metrics = [row["metrics"] for row in rows]
    if not metrics or any(not isinstance(item, dict) for item in metrics):
        raise ValueError("evaluation requires per-query metrics")
    return {
        key: sum(float(item[key]) for item in metrics if isinstance(item, dict))
        / len(metrics)
        for key in ("reciprocal_rank", "hit_at_1", "hit_at_3", "recall_at_3")
    }


async def evaluate_queries(
    index: LoadedIndex,
    embedder: AsyncCLIPEmbedder,
    reranker: AsyncLocalReranker | None,
    queries: tuple[tuple[str, ContentInput, tuple[str, ...]], ...],
    *,
    expand: bool,
) -> dict[str, object]:
    pipeline = AsyncSearchPipeline(
        docstore=index.docstore,
        vectorstore=index.vectorstore,
        embedder=embedder,
        reranker=reranker,
        strict_rerank=True,
    )
    rows: list[dict[str, object]] = []
    total = len(index.representations)
    for query_id, query, relevant in queries:
        started = time.perf_counter()
        result = await pipeline.run(
            query,
            mode="dense",
            top_k=total,
            recall_limit=total,
            rerank_limit=total,
            expand_parents=expand,
            context_budget=600,
        )
        elapsed = time.perf_counter() - started
        clip_tokens = (
            None
            if any(isinstance(part, ImagePart) for part in content_parts(query))
            else len(
                embedder.processor.tokenizer(
                    text_view(query), add_special_tokens=True, truncation=False
                )["input_ids"]
            )
        )
        rerank_lengths = (
            [
                len(
                    reranker.tokenizer(
                        text_view(query),
                        text_view(match.unit.content),
                        add_special_tokens=True,
                        truncation=False,
                    )["input_ids"]
                )
                for match in result.matched_units
            ]
            if reranker is not None
            else []
        )
        rows.append(
            {
                "query_id": query_id,
                "query_text": text_view(query),
                "query_image": any(
                    isinstance(part, ImagePart) for part in content_parts(query)
                ),
                "relevant_sources": list(relevant),
                "elapsed_seconds": elapsed,
                "clip_query_tokens": clip_tokens,
                "rerank_pairs": len(rerank_lengths),
                "rerank_pair_tokens_total": sum(rerank_lengths),
                "rerank_pair_tokens_max": max(rerank_lengths, default=0),
                **result_evidence(result, relevant),
            }
        )
    query_seconds = sum(cast(float, row["elapsed_seconds"]) for row in rows)
    return {
        "metrics": aggregate(rows),
        "query_total_seconds": query_seconds,
        "query_mean_seconds": query_seconds / len(rows),
        "queries": rows,
    }


def model_evidence(directory: Path, model_id: str, revision: str) -> dict[str, object]:
    return {
        "model_id": model_id,
        "revision": revision,
        "local_dir": str(directory.resolve()),
        "files": {
            path.name: {"sha256": file_hash(path), "size_bytes": path.stat().st_size}
            for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix in {".json", ".safetensors", ".txt"}
        },
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    torch = import_module("torch")

    validate_fixtures()
    metric_check = aggregate(
        [
            {
                "metrics": {
                    "reciprocal_rank": 0.5,
                    "hit_at_1": 0.0,
                    "hit_at_3": 1.0,
                    "recall_at_3": 1.0,
                }
            },
            {
                "metrics": {
                    "reciprocal_rank": 1.0,
                    "hit_at_1": 1.0,
                    "hit_at_3": 1.0,
                    "recall_at_3": 1.0,
                }
            },
        ]
    )
    if metric_check != {
        "reciprocal_rank": 0.75,
        "hit_at_1": 0.5,
        "hit_at_3": 1.0,
        "recall_at_3": 1.0,
    }:
        raise ValueError("evaluation metric aggregation self-check failed")
    total_started = time.perf_counter()
    torch.set_num_threads(args.threads)
    started = time.perf_counter()
    embedder = await AsyncCLIPEmbedder.load(
        args.clip_model_dir, model_id=CLIP_ID, revision=CLIP_REVISION, device="cpu"
    )
    clip_seconds = time.perf_counter() - started
    started = time.perf_counter()
    reranker = await AsyncLocalReranker.load(
        args.reranker_model_dir,
        model_id=RERANKER_ID,
        revision=RERANKER_REVISION,
        device="cpu",
        batch_size=16,
    )
    reranker_seconds = time.perf_counter() - started
    documents = text_documents()
    queries = tuple(
        (query.query_id, query.text, query.relevant_sources) for query in QUERIES
    )
    strategies: list[dict[str, object]] = []
    for name, builder in (
        ("flat", FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(320)))),
        (
            "parent_child",
            ParentChildIndexBuilder(
                parent=FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(320))),
                child=FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(96))),
            ),
        ),
    ):
        index = await load_index(documents, builder, embedder, f"eval_text_{name}")
        for enabled in (False, True):
            evaluation = await evaluate_queries(
                index,
                embedder,
                reranker if enabled else None,
                queries,
                expand=name == "parent_child",
            )
            strategies.append(
                {
                    "strategy": name,
                    "rerank": enabled,
                    "index": index.evidence,
                    **evaluation,
                }
            )
    images = image_documents(args.image_dir)
    image_queries: tuple[tuple[str, ContentInput, tuple[str, ...]], ...] = (
        ("cat_text", "a photograph of a cat", ("cat",)),
        ("dog_text", "a photograph of a dog", ("dog",)),
        *(
            (
                f"{doc.source.source_id}_image",
                (ImagePart(doc.assets[0].media),),
                (doc.source.source_id,),
            )
            for doc in images
        ),
    )
    modes: list[dict[str, object]] = []
    for mode in ("text", "media", "separate", "joint"):
        typed_mode: Literal["text", "media", "separate", "joint"] = mode
        builder = FlatIndexBuilder(
            DocumentChunker(RecursiveTextChunker(320)), representation=typed_mode
        )
        started = time.perf_counter()
        try:
            index = await load_index(images, builder, embedder, f"eval_image_{mode}")
        except ValueError as exc:
            if mode != "joint" or "joint input" not in str(exc):
                raise
            modes.append(
                {
                    "representation": mode,
                    "status": "unsupported",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "elapsed_seconds": time.perf_counter() - started,
                }
            )
            continue
        modes.append(
            {
                "representation": mode,
                "status": "supported",
                "index": index.evidence,
                **await evaluate_queries(
                    index, embedder, None, image_queries, expand=False
                ),
            }
        )
    try:
        await reranker.rerank(
            "a cat", (ContentItem("cat", (ImagePart(images[0].assets[0].media),)),)
        )
    except ValueError as exc:
        image_rerank = {
            "status": "unsupported",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    else:
        raise ValueError("local text reranker unexpectedly accepted an image")
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "evaluation_seconds_before_report_hashing": time.perf_counter() - total_started,
        "purpose": "Fixed-fixture offline quality and cost evidence, not a general quality benchmark.",
        "method": {
            "queries_fixed_before_model_execution": True,
            "relevance_level": "source_id",
            "source_ranking": "first occurrence after unit ranking; duplicate units do not count twice",
            "recall": "all available representation records",
            "retrieval": "dense only; no keyword channel",
            "rerank_depth": "all recalled units",
            "context_budget_chars": 600,
            "unit_selection": "all ranked units retained in raw evidence",
            "latency": "one CPU pass per condition; cold-load reported separately; no statistical latency claim",
            "vector_bytes": "float32 theoretical payload only, not Python or store RSS",
        },
        "limitations": [
            "Eight manually labeled text queries and twelve authored short documents are a small fixture, with no external dataset or confidence interval.",
            "Two labeled animal images only; image queries reuse indexed images and are identity checks, not generalization tests.",
            "Image text mode uses supplied truthful alt annotations; it does not perform OCR or caption generation.",
            "CLIP text embeddings are evaluated as provided; this is not a recommendation for text retrieval at scale.",
            "No persistent backend, network, concurrency, throughput, peak RSS, GPU, billing or answer quality was measured.",
            "Separate representations remain independent vectors; joint CLIP is unsupported and vectors are never averaged.",
        ],
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "device": "cpu",
            "torch_threads": args.threads,
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("torch", "transformers", "pillow")
            },
        },
        "models": {
            "clip": {
                **model_evidence(args.clip_model_dir, CLIP_ID, CLIP_REVISION),
                "load_seconds": clip_seconds,
                "space_id": embedder.space.space_id,
                "dimension": embedder.space.dimension,
            },
            "reranker": {
                **model_evidence(
                    args.reranker_model_dir, RERANKER_ID, RERANKER_REVISION
                ),
                "load_seconds": reranker_seconds,
            },
        },
        "text_fixture": {
            "corpus": CORPUS,
            "queries": [asdict(query) for query in QUERIES],
        },
        "text_comparisons": strategies,
        "image_fixture": [
            {
                "label": doc.source.source_id,
                "path": str(doc.assets[0].media.path),
                "sha256": doc.assets[0].content_hash,
                "size_bytes": doc.assets[0].size_bytes,
                "annotation": doc.blocks[0].alt_text,
            }
            for doc in images
        ],
        "representation_comparisons": modes,
        "image_reranker_boundary": image_rerank,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip-model-dir", required=True, type=Path)
    parser.add_argument("--reranker-model-dir", required=True, type=Path)
    parser.add_argument("--image-dir", required=True, type=Path)
    parser.add_argument("--report-path", required=True, type=Path)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if args.threads <= 0:
        parser.error("--threads must be positive")
    report = asyncio.run(run(args))
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report_path": str(args.report_path.resolve()),
                "text": [
                    {
                        key: row[key]
                        for key in (
                            "strategy",
                            "rerank",
                            "metrics",
                            "query_total_seconds",
                        )
                    }
                    for row in cast(list[dict[str, object]], report["text_comparisons"])
                ],
                "representations": [
                    {key: row[key] for key in ("representation", "status")}
                    for row in cast(
                        list[dict[str, object]], report["representation_comparisons"]
                    )
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
