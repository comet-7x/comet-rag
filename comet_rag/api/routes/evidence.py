from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from comet_rag.api.deps import ContextDep
from comet_rag.api.schemas.evidence import ParentEvidenceResponse
from comet_rag.services.evidence_access import EvidenceNotFound

router = APIRouter(prefix="/evidence", tags=["evidence"])
bearer = HTTPBearer(auto_error=False)


@router.get("/{kb_id}/parents/{parent_id}", response_model=ParentEvidenceResponse)
async def read_parent_evidence(
    kb_id: str,
    parent_id: str,
    ctx: ContextDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    response: Response,
) -> ParentEvidenceResponse:
    reader = ctx.evidence_reader
    if reader is None:
        raise EvidenceNotFound("证据入口未启用")
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            401,
            "需要证据读取凭据",
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    view = await reader.read(
        token=credentials.credentials, kb_id=kb_id, parent_id=parent_id
    )
    response.headers["Cache-Control"] = "no-store"
    return ParentEvidenceResponse.model_validate(asdict(view))
