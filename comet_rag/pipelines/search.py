from __future__ import annotations

from typing import Literal

from comet_rag.content import (
    ContentInput,
    ContentItem,
    ImagePart,
    content_parts,
    text_view,
)
from comet_rag.docstores.base import AsyncDocStore
from comet_rag.embedder.types import AsyncContentEmbedder
from comet_rag.rerankers import AsyncReranker
from comet_rag.retrieval.context import select_contexts
from comet_rag.retrieval.grouping import group_and_fuse
from comet_rag.retrieval.types import SearchMatch, SearchResult
from comet_rag.vectorstores.base import AsyncVectorStore
from comet_rag.vectorstores.filters import Filter, matches


class AsyncSearchPipeline:
    def __init__(
        self,
        *,
        docstore: AsyncDocStore,
        vectorstore: AsyncVectorStore,
        embedder: AsyncContentEmbedder | None = None,
        reranker: AsyncReranker | None = None,
        strict_rerank: bool = False,
    ) -> None:
        if (
            docstore.index_scope != vectorstore.index_scope
            or embedder is not None
            and embedder.space.space_id != vectorstore.space.space_id
        ):
            raise ValueError("search scope/space mismatch")
        self.docstore = docstore
        self.vectorstore = vectorstore
        self.embedder = embedder
        if type(strict_rerank) is not bool:
            raise ValueError("strict_rerank must be boolean")
        self.reranker = reranker
        self.strict_rerank = strict_rerank

    async def run(
        self,
        query: ContentInput,
        *,
        mode: Literal["dense", "keyword", "hybrid"] | None = None,
        keyword_text: str | None = None,
        top_k: int = 5,
        recall_limit: int = 40,
        rerank_limit: int = 20,
        expand_parents: bool = False,
        context_budget: int | None = None,
        filters: Filter | None = None,
    ) -> SearchResult:
        if (
            type(expand_parents) is not bool
            or context_budget is not None
            and (type(context_budget) is not int or context_budget < 0)
        ):
            raise ValueError("context budget must be nonnegative and expansion boolean")
        for value in (top_k, recall_limit, rerank_limit):
            if type(value) is not int or value <= 0:
                raise ValueError("search limits must be positive integers")
        parts = content_parts(query)
        has_image = any(isinstance(part, ImagePart) for part in parts)
        if mode is None:
            mode = "dense" if has_image else "hybrid"
        if mode not in ("dense", "keyword", "hybrid"):
            raise ValueError("unknown search mode")
        text = keyword_text if keyword_text is not None else text_view(query)
        if (
            not isinstance(text, str)
            or mode in ("keyword", "hybrid")
            and not text.strip()
        ):
            raise ValueError("keyword search requires an explicit nonblank text view")
        channels = {}
        if mode in ("dense", "hybrid"):
            if self.embedder is None:
                raise ValueError("dense search requires a query embedder")
            embeddings = await self.embedder.embed(
                (ContentItem("query", query),), role="query"
            )
            if (
                len(embeddings) != 1
                or embeddings[0].item_id != "query"
                or embeddings[0].space_id != self.vectorstore.space.space_id
            ):
                raise ValueError("query embedding identity/count/space mismatch")
            channels["dense"] = await self.vectorstore.search(
                embeddings[0], limit=recall_limit, filters=filters
            )
        if mode in ("keyword", "hybrid"):
            channels["keyword"] = await self.vectorstore.search_text(
                text, limit=recall_limit, filters=filters
            )
        for hits in channels.values():
            for hit in hits:
                if (hit.index_scope, hit.index_profile_id, hit.space_id) != (
                    self.vectorstore.index_scope,
                    self.vectorstore.index_profile_id,
                    self.vectorstore.space.space_id,
                ):
                    raise ValueError("retrieval returned a foreign scope/profile/space")
        order, evidence, scores = group_and_fuse(channels)
        # 在评分前核对可能参与评分或回退返回的完整内容与证据。
        selected = order[: max(top_k, rerank_limit) if self.reranker else top_k]
        units = await self.docstore.get_units(selected)
        if set(units) != set(selected):
            raise ValueError("required search content is missing")
        rep_ids = tuple(
            dict.fromkeys(
                item.hit.representation_id
                for unit_id in selected
                for item in evidence[unit_id]
            )
        )
        representations = await self.docstore.get_representations(rep_ids)
        if set(representations) != set(rep_ids):
            raise ValueError("required search representations are missing")
        for unit_id in selected:
            unit = units[unit_id]
            if unit.unit_id != unit_id or (unit.index_scope, unit.index_profile_id) != (
                self.vectorstore.index_scope,
                self.vectorstore.index_profile_id,
            ):
                raise ValueError("stored content identity/scope/profile mismatch")
            if not matches(unit, filters):
                raise ValueError("stored content does not satisfy search filters")
            for item in evidence[unit_id]:
                hit = item.hit
                rep = representations[hit.representation_id]
                if (
                    rep.representation_id != hit.representation_id
                    or (hit.unit_id, hit.document_id, hit.source_id)
                    != (
                        unit.unit_id,
                        unit.document_id,
                        unit.source_id,
                    )
                    or (
                        rep.unit_id,
                        rep.document_id,
                        rep.source_id,
                        rep.index_scope,
                        rep.index_profile_id,
                    )
                    != (
                        unit.unit_id,
                        unit.document_id,
                        unit.source_id,
                        unit.index_scope,
                        unit.index_profile_id,
                    )
                ):
                    raise ValueError("retrieval and stored content identities disagree")
        diagnostics: list[str] = []
        rerank_result = None
        rerank_scores = {}
        ranked = order
        if self.reranker is not None and order:
            candidates = tuple(
                ContentItem(item_id, units[item_id].content)
                for item_id in order[:rerank_limit]
            )
            try:
                rerank_result = await self.reranker.rerank(query, candidates)
                rerank_result.validate(candidates)
            except Exception as exc:
                rerank_result = None
                if self.strict_rerank:
                    raise
                diagnostics.append(f"rerank_failed:{type(exc).__name__}")
            else:
                ranked = tuple(item.candidate_id for item in rerank_result.items)
                rerank_scores = {
                    item.candidate_id: item.score for item in rerank_result.items
                }
                if len(ranked) < top_k and len(order) > len(ranked):
                    diagnostics.append("rerank_limit_restricts_matches")
        matched = tuple(
            SearchMatch(
                units[item_id],
                evidence[item_id],
                scores.get(item_id),
                rerank_scores.get(item_id),
            )
            for item_id in ranked[:top_k]
        )
        contexts, context_diagnostics = await select_contexts(
            tuple(match.unit for match in matched),
            docstore=self.docstore,
            expand_parents=expand_parents,
            budget=context_budget,
            filters=filters,
        )
        return SearchResult(
            matched,
            contexts,
            tuple(channels),
            rerank_result is not None,
            (*diagnostics, *context_diagnostics),
            rerank_result,
        )
