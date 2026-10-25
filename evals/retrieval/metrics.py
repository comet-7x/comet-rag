from __future__ import annotations

from math import ceil, log2
from statistics import mean
from typing import Annotated

from pydantic import Field, field_validator

from evals.retrieval.data import Dataset, Name, StrictModel, Text


class Hit(StrictModel):
    id: Text
    document_id: Name
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    text: Text


class QueryRun(StrictModel):
    query_id: Name
    hits: list[Hit]
    degraded: bool = False
    error: str | None = None
    elapsed_ms: Annotated[float, Field(ge=0)] | None = None
    reranked: bool | None = None
    fetched: Annotated[int, Field(ge=0)] | None = None


class Run(StrictModel):
    dataset_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    top_k: Annotated[int, Field(ge=5, le=100)]
    settings: dict[str, str | int | bool]
    results: list[QueryRun]

    @field_validator("settings")
    @classmethod
    def require_identity(
        cls, settings: dict[str, str | int | bool]
    ) -> dict[str, str | int | bool]:
        for key in (
            "model",
            "backend",
            "strategy",
            "mode",
            "level",
            "implementation_sha256",
        ):
            value = settings.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"settings 缺少 {key}")
        dim = settings.get("dim")
        if type(dim) is not int or dim <= 0 or type(settings.get("rerank")) is not bool:
            raise ValueError("settings 需要正整数 dim 和布尔 rerank")
        return settings


def score(dataset: Dataset, run: Run) -> dict[str, object]:
    """以返回位置计排名；重复文档占位但只记一次收益，不拼接碎片伪造证据。"""
    if run.dataset_sha256 != dataset.fingerprint:
        raise ValueError("dataset fingerprint 不匹配")
    queries = {q.id: q for q in dataset.judgments.queries}
    if len(run.results) != len(queries) or {r.query_id for r in run.results} != set(
        queries
    ):
        raise ValueError("run 必须恰好包含每个 query 一次")
    sources = {s.id: s for s in dataset.corpus.documents}
    evidence = {e.id: e for e in dataset.judgments.evidence}
    rows: list[dict[str, object]] = []
    aggregates: dict[str, list[float]] = {}
    no_answer: list[float] = []
    latencies = sorted(r.elapsed_ms for r in run.results if r.elapsed_ms is not None)
    for result in run.results:
        query = queries[result.query_id]
        if result.error is not None or result.degraded:
            raise ValueError(f"query {query.id} 失败或降级；不能计为健康质量基线")
        if run.settings["rerank"] and result.hits and result.reranked is not True:
            raise ValueError("请求重排但缺少实际完成重排的证据")
        if not run.settings["rerank"] and result.reranked is True:
            raise ValueError("实际重排与 run 配置不一致")
        if len(result.hits) > run.top_k or len({h.id for h in result.hits}) != len(
            result.hits
        ):
            raise ValueError("超出 top_k 或重复 hit id")
        for hit in result.hits:
            source = sources.get(hit.document_id)
            if source is None or source.kb_id != query.kb_id or not source.active:
                raise ValueError(f"query {query.id} 命中未知、跨 KB 或 inactive 文档")
            text = dataset.documents[hit.document_id].markdown
            if (
                hit.end > len(text)
                or hit.start >= hit.end
                or text[hit.start : hit.end] != hit.text
            ):
                raise ValueError("返回正文必须与规范文档 span 完全一致")
        row: dict[str, object] = {"query_id": query.id, "category": query.category}
        row["elapsed_ms"] = result.elapsed_ms
        row["reranked"] = result.reranked
        row["fetched"] = result.fetched
        positive = {did for did, grade in query.relevance.items() if grade > 0}
        for k in (1, 3, 5):
            hits = result.hits[:k]
            names = ("recall", "rr", "ndcg", "evidence")
            if not positive:
                row.update({f"{name}@{k}": None for name in names})
                row[f"covered_evidence@{k}"] = []
                continue
            seen: set[str] = set()
            gains: list[int] = []
            relevant_ranks: list[int] = []
            for rank, hit in enumerate(hits, 1):
                grade = (
                    0
                    if hit.document_id in seen
                    else query.relevance.get(hit.document_id, 0)
                )
                seen.add(hit.document_id)
                gains.append(2**grade - 1)
                if grade > 0:
                    relevant_ranks.append(rank)
            ideal = sorted(
                (2**g - 1 for g in query.relevance.values() if g > 0), reverse=True
            )[:k]
            dcg = sum(gain / log2(rank + 1) for rank, gain in enumerate(gains, 1))
            idcg = sum(gain / log2(rank + 1) for rank, gain in enumerate(ideal, 1))
            covered = [
                eid
                for eid in query.evidence
                if any(
                    hit.document_id == evidence[eid].document_id
                    and hit.start <= dataset.spans[eid][0]
                    and hit.end >= dataset.spans[eid][1]
                    for hit in hits
                )
            ]
            row[f"covered_evidence@{k}"] = covered
            values = (
                len(positive & seen) / len(positive),
                1 / relevant_ranks[0] if relevant_ranks else 0.0,
                dcg / idcg,
                len(covered) / len(query.evidence),
            )
            for name, value in zip(names, values, strict=True):
                metric = f"{name}@{k}"
                row[metric] = value
                aggregates.setdefault(metric, []).append(value)
        row["returned_chars@5"] = sum(len(h.text) for h in result.hits[:5])
        if not positive:
            no_answer.append(float(bool(result.hits)))
        rows.append(row)
    return {
        "dataset_sha256": dataset.fingerprint,
        "settings": run.settings,
        "queries": len(queries),
        "positive_queries": len(queries) - len(no_answer),
        "no_answer_queries": len(no_answer),
        "macro": {name: mean(values) for name, values in aggregates.items()},
        "no_answer_return_rate@5": mean(no_answer) if no_answer else None,
        "mean_returned_chars@5": mean(
            sum(len(h.text) for h in r.hits[:5]) for r in run.results
        ),
        "per_query": rows,
        "latency_ms": {
            "samples": len(latencies),
            **{
                name: latencies[ceil(len(latencies) * q) - 1] if latencies else None
                for name, q in (("p50", 0.5), ("p95", 0.95))
            },
        },
    }
