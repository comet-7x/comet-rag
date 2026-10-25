from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from typing import Any

from comet_rag.ports.content import ContentInput, RankedDocument, RerankDocument
from comet_rag.ports.gate import AsyncGate
from comet_rag.ports.reranker import AsyncRerankerPort
from comet_rag.services.reranking import validate_ranking
from evals.retrieval.baseline import implementation_fingerprint
from evals.retrieval.data import Dataset
from evals.retrieval.metrics import Hit, QueryRun, Run, score
from evals.retrieval.title_context import TitlePolicy, title_view

POLICIES: tuple[TitlePolicy, ...] = ("plain", "headings", "ancestors")


@dataclass(frozen=True, slots=True)
class CandidatePool:
    query_id: str
    query: str
    documents: tuple[RerankDocument, ...]


class AsyncRecordingReranker:
    """顺序采集实际生产重排输入；借用客户端，关闭权仍归调用方。"""

    def __init__(self, delegate: AsyncRerankerPort) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, tuple[RerankDocument, ...]]] = []

    async def rank(
        self,
        query: ContentInput,
        documents: Sequence[str | RerankDocument],
        /,
        *,
        top_k: int | None = None,
        **kwargs: Any,
    ) -> list[RankedDocument]:
        # Any 只转发既有 Port 的供应商参数；实验不解释或保存凭据。
        if not isinstance(query, str) or any(
            not isinstance(d, RerankDocument) or not isinstance(d.content, str)
            for d in documents
        ):
            raise ValueError("标题对照只采集带身份的纯文本候选")
        snapshot = tuple(
            deepcopy(d) for d in documents if isinstance(d, RerankDocument)
        )
        self.calls.append((query, snapshot))
        return await self.delegate.rank(query, documents, top_k=top_k, **kwargs)

    def bind_gate(self, gate: AsyncGate | None) -> None:
        self.delegate.bind_gate(gate)

    async def close(self) -> None:
        """非拥有者不关闭底层共享模型。"""

    def pools(self, dataset: Dataset, capture: Run) -> tuple[CandidatePool, ...]:
        score(dataset, capture)
        if capture.settings["rerank"] is not True:
            raise ValueError("候选采集必须实际请求重排")
        calls = iter(self.calls)
        pools: list[CandidatePool] = []
        for query, result in zip(
            dataset.judgments.queries, capture.results, strict=True
        ):
            if result.query_id != query.id:
                raise ValueError("采集顺序与查询身份不一致")
            if not result.hits:
                pools.append(CandidatePool(query.id, query.text, ()))
                continue
            call = next(calls, None)
            if call is None or call[0] != query.text or len(call[1]) != result.fetched:
                raise ValueError("实际重排输入与采集记录不一致")
            pools.append(CandidatePool(query.id, query.text, call[1]))
        if next(calls, None) is not None:
            raise ValueError("存在未配对的重排调用")
        return tuple(pools)


def pool_payload(pools: Sequence[CandidatePool]) -> list[dict[str, object]]:
    return [
        {
            "query_id": p.query_id,
            "query": p.query,
            "documents": [
                {"id": d.id, "text": d.content, "metadata": dict(d.metadata)}
                for d in p.documents
            ],
        }
        for p in pools
    ]


def pool_fingerprint(pools: Sequence[CandidatePool]) -> str:
    encoded = json.dumps(pool_payload(pools), ensure_ascii=False, sort_keys=True)
    return sha256(encoded.encode()).hexdigest()


def _hits(dataset: Dataset, pool: CandidatePool) -> list[Hit]:
    identities = {
        (s.kb_id, s.source_id, s.revision): s.id for s in dataset.corpus.documents
    }
    hits: list[Hit] = []
    for document in pool.documents:
        md = document.metadata
        identity = (md["kb_id"], md["source_id"], md["document_revision"])
        hits.append(
            Hit.model_validate(
                {
                    "id": document.id,
                    "document_id": identities[identity],
                    "start": md["chunk_start"],
                    "end": md["chunk_end"],
                    "text": document.content,
                }
            )
        )
    # 全部候选先核验身份与原文；最终前五另外交给既有严格评分器。
    query = next(q for q in dataset.judgments.queries if q.id == pool.query_id)
    sources = {s.id: s for s in dataset.corpus.documents}
    if pool.query != query.text or len({h.id for h in hits}) != len(hits):
        raise ValueError("候选池查询或候选身份不唯一")
    for hit in hits:
        source = sources[hit.document_id]
        canonical = dataset.documents[hit.document_id].markdown
        if (
            not source.active
            or source.kb_id != query.kb_id
            or canonical[hit.start : hit.end] != hit.text
            or not 0 <= hit.start < hit.end <= len(canonical)
        ):
            raise ValueError("候选池含跨界、旧版或非原文候选")
    return hits


async def replay(
    dataset: Dataset,
    capture: Run,
    pools: Sequence[CandidatePool],
    reranker: AsyncRerankerPort,
) -> dict[TitlePolicy, Run]:
    """冻结候选后交错执行三个输入视图；返回正文与证据始终按原始区间计。"""
    score(dataset, capture)
    if [p.query_id for p in pools] != [q.id for q in dataset.judgments.queries]:
        raise ValueError("候选池必须按顺序恰好包含每个查询一次")
    fingerprint = pool_fingerprint(pools)
    rows: dict[TitlePolicy, list[QueryRun]] = {p: [] for p in POLICIES}
    added = dict.fromkeys(POLICIES, 0)
    applied = dict.fromkeys(POLICIES, 0)
    for offset, pool in enumerate(pools):
        hits = _hits(dataset, pool)
        for policy in POLICIES[offset % 3 :] + POLICIES[: offset % 3]:
            views: list[RerankDocument] = []
            for document in pool.documents:
                if not isinstance(document.content, str):
                    raise ValueError("标题对照只接受文本")
                view = title_view(document.content, document.metadata, policy)
                added[policy] += len(view) - len(document.content)
                applied[policy] += int(view != document.content)
                views.append(
                    RerankDocument(
                        id=document.id,
                        content=view,
                        metadata=deepcopy(document.metadata),
                    )
                )
            started = perf_counter()
            if views:
                ranked = validate_ranking(
                    await reranker.rank(pool.query, views), candidate_count=len(views)
                )
                selected = [hits[item.index] for item in ranked[: capture.top_k]]
            else:
                selected = []
            rows[policy].append(
                QueryRun(
                    query_id=pool.query_id,
                    hits=selected,
                    reranked=bool(views),
                    fetched=len(views),
                    elapsed_ms=(perf_counter() - started) * 1000,
                )
            )
    if pool_fingerprint(pools) != fingerprint:
        raise ValueError("重排修改了冻结候选池")
    runs: dict[TitlePolicy, Run] = {}
    for policy in POLICIES:
        settings = dict(capture.settings)
        settings.update(
            title_policy=policy,
            pool_sha256=fingerprint,
            timing_scope="reranker replay only; not retrieval latency",
            applied_views=applied[policy],
            added_input_chars=added[policy],
            implementation_sha256=implementation_fingerprint(),
        )
        run = Run(
            dataset_sha256=dataset.fingerprint,
            top_k=capture.top_k,
            settings=settings,
            results=rows[policy],
        )
        score(dataset, run)
        runs[policy] = run
    return runs
