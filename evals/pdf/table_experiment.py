from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from comet_rag.composition.bootstrap import (
    build_embedding_model,
    build_model_gate,
    build_reranker,
)
from comet_rag.config.schemas import APPConfig
from comet_rag.services.retrieval import ReturnLevel, SearchMode
from evals.pdf.artifacts import FROZEN, load_frozen
from evals.pdf.inspect import table_integrity
from evals.pdf.real import _counts, _probe
from evals.pdf.table_data import CASES, ROW_SEPARATORS, joint_report, load_cases
from evals.pdf.tokens import BodyTokens, body_budget
from evals.retrieval.baseline import Strategy, collect, implementation_fingerprint
from evals.retrieval.data import write_json

VARIANTS: tuple[tuple[Strategy, ReturnLevel, bool], ...] = (
    ("recursive", ReturnLevel.CHILD, False),
    ("page", ReturnLevel.CHILD, False),
    ("page", ReturnLevel.CHILD, True),
    ("hierarchy", ReturnLevel.CHILD, False),
    ("hierarchy", ReturnLevel.CHILD, True),
    ("hierarchy", ReturnLevel.PARENT, False),
    ("hierarchy", ReturnLevel.PARENT, True),
)


async def evaluate(
    config: APPConfig, output: Path, *, frozen: Path = FROZEN, cases: Path = CASES
) -> None:
    """固定作者暂定表格题的局部探索；不批准生产策略或独立质量验收。"""
    dataset = load_cases(frozen=frozen, cases=cases)
    full, canonical = load_frozen(frozen)
    settings = config.infrastructure_config.reranker
    if settings is None:
        raise ValueError("表格实验需要真实reranker及其原生tokenize服务")
    output.mkdir(parents=True, exist_ok=False)
    source_hash = implementation_fingerprint()
    write_json(
        output / "frozen.json",
        {
            "dataset_sha256": dataset.fingerprint,
            "full_canonical_sha256": canonical,
            "implementation_sha256": source_hash,
            "label_status": "author_provisional",
            "independent_review": "pending",
            "model_artifacts": "unknown",
            "query_ids": [q.id for q in dataset.judgments.queries],
            "scope": "selected author-provisional table queries; not a full original-question evaluation",
        },
    )
    write_json(
        output / "structure.json",
        {
            profile: {
                name: table_integrity(
                    doc, separators=ROW_SEPARATORS if profile == "rows" else None
                )
                for name, doc in full.items()
            }
            for profile in ("default", "rows")
        },
    )
    summary = {}
    cache: dict[str, BodyTokens] = {}
    async with AsyncExitStack() as stack:
        embedding = build_embedding_model(config)
        stack.push_async_callback(embedding.close)
        reranker = build_reranker(config)
        if reranker is None:
            raise ValueError("表格实验未装配reranker")
        stack.push_async_callback(reranker.close)
        gate = build_model_gate(config)
        embedding.bind_gate(gate)
        reranker.bind_gate(gate)
        counter = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=settings.base_url.rstrip("/").removesuffix("/v1") + "/",
                headers={
                    "Authorization": "Bearer " + (settings.api_key_value or "EMPTY")
                },
                timeout=30,
            )
        )
        before = await _probe(counter, settings.model_name)
        for strategy, level, use_rows in VARIANTS:
            for rerank in (False, True):
                name = f"{strategy}-{level.value}-{'rows' if use_rows else 'default'}-rerank-{str(rerank).lower()}"
                model = config.infrastructure_config.embedding_model
                run = await collect(
                    dataset,
                    strategy=strategy,
                    mode=SearchMode.HYBRID,
                    level=level,
                    embedding_model=embedding,
                    model_name=model.model_name,
                    embedding_dim=model.dim,
                    reranker=reranker,
                    rerank=rerank,
                    model_revision="unknown",
                    child_size=800,
                    child_overlap=80,
                    parent_size=1600,
                    separators=ROW_SEPARATORS if use_rows else None,
                )
                if run.settings["implementation_sha256"] != source_hash:
                    raise ValueError("实验期间源码发生变化，不能发布同源对照")
                run.settings.update(
                    label_status="author-provisional native table cases; independent review pending",
                    extraction_scope="frozen MinerU selected 12 pages; complete 87-page structural audit separate",
                    reranker=settings.model_name if rerank else "disabled",
                    token_probe_sha256=before["sha256"],
                )
                counts = await _counts(counter, settings.model_name, run, cache)
                write_json(
                    output / f"{name}.tokens.json",
                    {
                        key: {"text_sha256": value.text_sha256, "count": value.count}
                        for key, value in counts.items()
                    },
                )
                for suffix, projected in [
                    ("top5", run),
                    *[
                        (
                            f"body{limit}",
                            body_budget(
                                run,
                                counts,
                                limit=limit,
                                tokenizer_model=settings.model_name,
                                tokenizer_revision="unknown",
                            ),
                        )
                        for limit in (512, 1024)
                    ],
                ]:
                    report = joint_report(dataset, projected)
                    key = f"{name}-{suffix}"
                    write_json(output / f"{key}.run.json", projected.model_dump())
                    write_json(output / f"{key}.report.json", report)
                    summary[key] = {
                        "settings": report["settings"],
                        "macro": report["macro"],
                        "joint_units": report["joint_units"],
                        "quality_acceptance": report["quality_acceptance"],
                    }
                print(name, "completed", flush=True)
        after = await _probe(counter, settings.model_name)
        if before != after:
            raise ValueError("计数探针变化，不能发布同预算摘要")
        write_json(output / "token-probes.json", {"before": before, "after": after})
    if implementation_fingerprint() != source_hash:
        raise ValueError("实验结束前源码发生变化，不能发布对照摘要")
    write_json(output / "summary.json", summary)


def main() -> None:
    from comet_rag.config.settings import YamlConfigLoader

    parser = argparse.ArgumentParser(description="原生表格题的局部探索，非独立质量验收")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(evaluate(YamlConfigLoader.get_config(args.config), args.out))


if __name__ == "__main__":
    main()
