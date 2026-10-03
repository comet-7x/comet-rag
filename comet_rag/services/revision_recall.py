from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from comet_rag.core.logging import logger
from comet_rag.ports import SearchHit
from comet_rag.ports.document_store import DocumentStore

DEFAULT_MAX_REVISION_CANDIDATES = 2048


class RevisionVisibilityError(RuntimeError):
    """无法确定 active 指针时必须拒绝查询，不能当作可降级的通道故障。"""


@dataclass(frozen=True, slots=True)
class VisibleRecall:
    hits: list[SearchHit]
    diagnostics: tuple[str, ...] = ()


class RevisionRecall:
    """每通道至多两次召回；判活源自 DocumentStore，而非向量中的状态标记。"""

    def __init__(
        self,
        document_store: DocumentStore,
        *,
        max_candidates: int = DEFAULT_MAX_REVISION_CANDIDATES,
    ) -> None:
        if (
            not isinstance(max_candidates, int)
            or isinstance(max_candidates, bool)
            or not 1 <= max_candidates <= 2048
        ):
            raise ValueError("max_candidates 必须在 1..2048 范围内")
        self._store = document_store
        self._cap = max_candidates

    async def arecall(
        self,
        kb_id: str,
        *,
        fetch_k: int,
        recall: Callable[[int], Awaitable[list[SearchHit]]],
    ) -> VisibleRecall:
        initial_limit = min(max(fetch_k * 4, 64), self._cap)
        candidates: dict[str, SearchHit] = {}
        diagnostics: set[str] = set()
        visible: list[SearchHit] = []
        for limit in dict.fromkeys((initial_limit, self._cap)):
            hits = await recall(limit)
            overflow = False
            # 保留首轮的高排位；即使 ANN 两轮不稳定，也不能突破检查预算。
            for hit in hits[:limit]:
                if hit.id not in candidates:
                    if len(candidates) < self._cap:
                        candidates[hit.id] = hit
                    else:
                        overflow = True
            visible, damaged = await self._visible(kb_id, list(candidates.values()))
            if damaged:
                diagnostics.add("invalid_revision_metadata")
            if len(visible) < fetch_k and (
                overflow or (limit == self._cap and len(hits) >= limit)
            ):
                diagnostics.add("revision_candidate_cap")
            if len(visible) >= fetch_k or len(hits) < limit:
                break
        if diagnostics:
            logger.warning(
                "revision 召回质量退化 reasons={} checked={} visible={}",
                sorted(diagnostics),
                len(candidates),
                len(visible),
            )
        return VisibleRecall(visible[:fetch_k], tuple(sorted(diagnostics)))

    async def _visible(
        self, kb_id: str, hits: list[SearchHit]
    ) -> tuple[list[SearchHit], bool]:
        valid: list[SearchHit] = []
        sources: dict[str, None] = {}
        damaged = False
        for hit in hits:
            source = hit.metadata.get("source_id")
            revision = hit.metadata.get("document_revision")
            # 缺失 revision 才是 legacy；null/空值不能伪装成 legacy。
            has_revision = "document_revision" in hit.metadata
            if (
                not isinstance(source, str)
                or not source.strip()
                or len(source) > 64
                or (
                    has_revision
                    and (
                        not isinstance(revision, str)
                        or not revision.strip()
                        or len(revision) > 64
                    )
                )
                or hit.metadata.get("kb_id", kb_id) != kb_id
            ):
                damaged = True
                continue
            sources[source] = None
            valid.append(hit)
        if not sources:
            return [], damaged
        try:
            active = await self._store.aget_active_revisions(kb_id, list(sources))
        except Exception as exc:  # noqa: BLE001 — 判活失败绝不 fail-open
            logger.opt(exception=exc).error("revision 判活存储不可用")
            raise RevisionVisibilityError("revision visibility unavailable") from exc
        for source in sources:
            if source not in active:
                raise RevisionVisibilityError("incomplete active revision lookup")
            revision = active[source]
            if revision is not None and (
                not isinstance(revision, str)
                or not revision.strip()
                or len(revision) > 64
            ):
                raise RevisionVisibilityError("invalid active revision lookup")
        return [
            hit
            for hit in valid
            if hit.metadata.get("document_revision")
            == active[hit.metadata["source_id"]]
        ], damaged
