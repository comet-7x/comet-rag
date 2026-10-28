from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class EvidenceFragment(BaseModel):
    pointer: str
    parent_pointer: str
    page_number: int
    block_type: str
    role: str
    category: Literal["extraction", "auxiliary"]
    start_char: int
    end_char: int
    heading_path: tuple[str, ...]
    bbox: tuple[float, float, float, float] | None
    continues_prev: bool


class ParentEvidenceResponse(BaseModel):
    """对外字段单独列出，领域账本将来增加私有事实时不会自动穿透API。"""

    kb_id: str
    source_id: str
    revision_id: str
    parent_id: str
    canonical_sha256: str
    start_char: int
    end_char: int
    text: str
    evidence: tuple[EvidenceFragment, ...]
