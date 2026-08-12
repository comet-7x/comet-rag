"""检索。"""

from fastapi import APIRouter

from ...schemas.search import SearchRequest, SearchResponse, SearchResultItem
from ...services.retrieval import SearchQuery
from ..deps import RetrievalDep

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(payload: SearchRequest, retrieval: RetrievalDep) -> SearchResponse:
    result = await retrieval.search(
        SearchQuery(
            kb_id=payload.kb_id,
            query=payload.query,
            top_k=payload.top_k,
            fetch_k=payload.fetch_k,
            filter=payload.filter,
            rerank=payload.rerank,
        )
    )
    return SearchResponse(
        chunks=[SearchResultItem(**c.to_dict()) for c in result.chunks],
        reranked=result.reranked,
        fetched=result.fetched,
        effective_top_k=result.effective_top_k,
        degraded=result.degraded,
    )
