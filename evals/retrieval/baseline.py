from __future__ import annotations

import re
import sys
from contextlib import AsyncExitStack
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from comet_rag.engines.chunkers import RecursiveChunker
from comet_rag.engines.chunkers.strategies import MarkdownSectionStrategy
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.infrastructure.persistence.document_store import InMemoryDocumentStore
from comet_rag.infrastructure.persistence.knowledge_base import (
    InMemoryKnowledgeBaseRepository,
)
from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import AsyncKeywordSearchPort, AsyncVectorStore
from comet_rag.ports.document_store import RevisionKey
from comet_rag.ports.embedding import AsyncEmbeddingPort
from comet_rag.ports.reranker import AsyncRerankerPort
from comet_rag.services.indexing import IndexPlanner, RevisionStager
from comet_rag.services.knowledge_base import KnowledgeBaseService, KnowledgeBaseSpec
from comet_rag.services.retrieval import (
    RetrievalService,
    ReturnLevel,
    SearchMode,
    SearchQuery,
)
from evals.retrieval.data import Dataset
from evals.retrieval.metrics import Hit, QueryRun, Run

Strategy = Literal["recursive", "markdown", "hierarchy"]
DIM = 256
MODEL = "offline-char-bigram-sha256-v1"
CHILD_SIZE = 100
PARENT_SIZE = 300
OVERLAP = 15


def implementation_fingerprint() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = sha256()
    for folder in (root / "comet_rag", root / "evals"):
        for path in sorted(folder.rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class AsyncHashEmbedding(AsyncBaseEmbeddingModel):
    """无语义能力的确定性替身；只读输入文本，不读取查询标签。"""

    async def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        # Any 仅承接模型 Port 的供应商可选参数，离线替身不解释它们。
        text = re.sub(r"\s+", "", data.casefold())
        vector = [0.0] * DIM
        for start in range(max(len(text) - 1, 1)):
            token = text[start : start + 2]
            bucket = int.from_bytes(sha256(token.encode()).digest()[:4]) % DIM
            vector[bucket] += 1.0
        return vector


async def collect(
    dataset: Dataset,
    *,
    strategy: Strategy,
    mode: SearchMode,
    level: ReturnLevel = ReturnLevel.CHILD,
    embedding_model: AsyncEmbeddingPort | None = None,
    model_name: str = MODEL,
    embedding_dim: int = DIM,
    reranker: AsyncRerankerPort | None = None,
    rerank: bool = False,
    model_revision: str = "fixed algorithm v1",
    vector_store: AsyncVectorStore | None = None,
    backend_name: str | None = None,
) -> Run:
    if level is ReturnLevel.PARENT and strategy != "hierarchy":
        raise ValueError("只有 hierarchy 基线比较真实父块回填")
    if rerank and reranker is None:
        raise ValueError("rerank 评测必须提供真实 reranker")
    if vector_store is not None and not backend_name:
        raise ValueError("外部评测存储必须声明后端身份")
    async with AsyncExitStack() as stack:
        vectors = (
            vector_store if vector_store is not None else AsyncInMemoryVectorStore()
        )
        if not isinstance(vectors, AsyncKeywordSearchPort):
            raise ValueError("评测存储必须提供关键词召回")
        documents = InMemoryDocumentStore()
        model = embedding_model or AsyncHashEmbedding()
        if embedding_model is None:
            stack.push_async_callback(model.close)
        if vector_store is None:
            stack.push_async_callback(vectors.close)
        stack.push_async_callback(documents.close)
        kb = KnowledgeBaseService(
            repository=InMemoryKnowledgeBaseRepository(),
            vector_store=vectors,
            embedding_model=model_name,
            embedding_dim=embedding_dim,
        )
        for kb_id in sorted({s.kb_id for s in dataset.corpus.documents}):
            await kb.create(KnowledgeBaseSpec(kb_id=kb_id))
        stager = RevisionStager(
            document_store=documents, vector_store=vectors, embedding_model=model
        )
        identities: dict[tuple[str, str, str], str] = {}
        child_count = parent_count = child_chars = parent_chars = 0
        child_lengths: list[int] = []
        # 旧版本先激活，随后被新版本替换；故意不 GC，验证可见性而不是物理删除。
        for source in sorted(dataset.corpus.documents, key=lambda s: s.active):
            doc = dataset.documents[source.id]
            child = RecursiveChunker(CHILD_SIZE, OVERLAP)
            if strategy == "hierarchy":
                chunks = HierarchyBuilder(RecursiveChunker(PARENT_SIZE), child).split(
                    doc
                )
            elif strategy == "markdown":
                chunks = MarkdownSectionStrategy(child).split(doc)
            else:
                chunks = child.split(doc.markdown)
            key = RevisionKey(source.kb_id, source.source_id, source.revision)
            plan = IndexPlanner().plan(
                chunks,
                key=key,
                source=f"{source.id}.{source.format}",
                file_type=source.format,
            )
            await stager.stage(plan, embedding_dim=embedding_dim)
            await documents.activate_revision(key)
            identities[(source.kb_id, source.source_id, source.revision)] = source.id
            child_count += len(plan.vector_children)
            parent_count += len(plan.parents)
            child_chars += sum(len(c.text) for c in plan.vector_children)
            child_lengths.extend(len(c.text) for c in plan.vector_children)
            parent_chars += sum(len(p.text) for p in plan.parents)
        retrieval = RetrievalService(
            embedding_model=model,
            vector_store=vectors,
            keyword_search=vectors,
            knowledge_base=kb,
            document_store=documents,
            reranker=reranker,
        )
        results: list[QueryRun] = []
        for query in dataset.judgments.queries:
            started = perf_counter()
            result = await retrieval.search(
                SearchQuery(
                    kb_id=query.kb_id,
                    query=query.text,
                    top_k=5,
                    fetch_k=50,
                    mode=mode,
                    return_level=level,
                    rerank=rerank,
                )
            )
            elapsed_ms = (perf_counter() - started) * 1000
            hits: list[Hit] = []
            for hit in result.chunks:
                md = hit.metadata
                identity = (
                    str(md["kb_id"]),
                    str(md["source_id"]),
                    str(md["document_revision"]),
                )
                # 不用 text.find 猜 offset；类型/未知身份错误必须中止，不能过滤坏结果。
                hits.append(
                    Hit.model_validate(
                        {
                            "id": hit.id,
                            "document_id": identities[identity],
                            "start": md["chunk_start"],
                            "end": md["chunk_end"],
                            "text": hit.text,
                        }
                    )
                )
            results.append(
                QueryRun(
                    query_id=query.id,
                    hits=hits,
                    degraded=result.degraded is not None or bool(result.degradations),
                    elapsed_ms=elapsed_ms,
                    reranked=result.reranked,
                    fetched=result.fetched,
                )
            )
        return Run(
            dataset_sha256=dataset.fingerprint,
            top_k=5,
            settings={
                "model": model_name,
                "model_revision": model_revision,
                "dim": embedding_dim,
                "backend": backend_name or "InMemory",
                "rerank": rerank,
                "strategy": strategy,
                "mode": mode.value,
                "level": level.value,
                "child_size": CHILD_SIZE,
                "child_overlap": OVERLAP,
                "parent_size": PARENT_SIZE,
                "fetch_k": 50,
                "indexed_children": child_count,
                "stored_parents": parent_count,
                "indexed_text_chars": child_chars,
                "child_chars_min": min(child_lengths),
                "child_chars_max": max(child_lengths),
                "parent_text_chars": parent_chars,
                "scope": "offline regression only"
                if embedding_model is None
                else "synthetic corpus, real model, exact InMemory recall"
                if vector_store is None
                else f"synthetic corpus, real model, native {backend_name} recall",
                "implementation_sha256": implementation_fingerprint(),
                "python": sys.version.split()[0],
                "python_docx": version("python-docx"),
            },
            results=results,
        )
