from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256

from comet_rag.core.logging import logger
from comet_rag.ports.document_store import DocumentStore
from comet_rag.ports.evidence import AsyncEvidenceStore, EvidenceSpan, evidence_digest
from comet_rag.services.evidence import (
    PARENT_EVIDENCE_KEY,
    ParentEvidenceReference,
    read_evidence,
)


class EvidenceAccessDenied(PermissionError):
    pass


class EvidenceNotFound(LookupError):
    pass


class EvidenceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceReadGrant:
    """服务端批准的token摘要与KB范围；不使用来源摘要、URL或父块ID充当访问凭据。"""

    token_sha256: str
    kb_ids: frozenset[str]

    def __post_init__(self) -> None:
        evidence_digest(self.token_sha256)
        if (
            not isinstance(self.kb_ids, frozenset)
            or not self.kb_ids
            or any(
                not isinstance(k, str) or not k.strip() or len(k) > 128
                for k in self.kb_ids
            )
        ):
            raise ValueError("证据读取许可需要明确的KB范围")
        object.__setattr__(self, "kb_ids", frozenset(self.kb_ids))


@dataclass(frozen=True, slots=True)
class ParentEvidenceView:
    kb_id: str
    source_id: str
    revision_id: str
    parent_id: str
    canonical_sha256: str
    start_char: int
    end_char: int
    text: str
    evidence: tuple[EvidenceSpan, ...]


class EvidenceReader:
    """只读active父块的原文证据；鉴权先于任何Store IO，不提供原始资产下载。"""

    def __init__(
        self,
        *,
        documents: DocumentStore,
        stores: Mapping[str, AsyncEvidenceStore],
        grants: Sequence[EvidenceReadGrant],
        timeout_seconds: float = 30,
    ) -> None:
        if not 0 < len(stores) <= 32 or not 0 < len(grants) <= 1000:
            raise ValueError("授权证据入口必须配置存储和读取许可")
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 120:
            raise ValueError("证据读取deadline须在(0,120]秒内")
        self._documents, self._stores = documents, dict(stores)
        self._grants, self._timeout = tuple(grants), timeout_seconds

    def authorize(self, token: str, kb_id: str) -> None:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise EvidenceAccessDenied("证据读取未获授权")
        token_hash = sha256(token.encode()).hexdigest()
        permitted = False
        for grant in self._grants:
            if (
                secrets.compare_digest(token_hash, grant.token_sha256)
                and kb_id in grant.kb_ids
            ):
                permitted = True
        if not permitted:
            raise EvidenceAccessDenied("证据读取未获授权")

    async def read(
        self, *, token: str, kb_id: str, parent_id: str
    ) -> ParentEvidenceView:
        self.authorize(token, kb_id)
        if not isinstance(parent_id, str) or not 1 <= len(parent_id) <= 64:
            raise EvidenceNotFound("证据不存在")
        try:
            async with asyncio.timeout(self._timeout):
                parents = await self._documents.get_parents(kb_id, [parent_id])
                parent = parents.get(parent_id)
                if parent is None:
                    raise EvidenceNotFound("证据不存在")
                if parent.key.kb_id != kb_id or parent.parent_id != parent_id:
                    raise ValueError("父块身份不匹配")
                active = await self._documents.get_active_revisions(
                    kb_id, [parent.key.source_id]
                )
                if parent.key.source_id not in active:
                    raise ValueError("判活结果缺失")
                if active[parent.key.source_id] != parent.key.revision_id:
                    raise EvidenceNotFound("证据不存在")
                reference = ParentEvidenceReference.from_payload(
                    parent.metadata.get(PARENT_EVIDENCE_KEY)
                )
                if reference.bundle.key != parent.key:
                    raise ValueError("父块引用其他revision")
                verified = await read_evidence(self._stores, reference)
                text = verified.projection.document.markdown
                if (
                    not 0 <= parent.start_char < parent.end_char <= len(text)
                    or text[parent.start_char : parent.end_char] != parent.text
                    or len(parent.text.encode()) > 256 * 1024
                ):
                    raise ValueError("父块原文或读取预算不匹配")
                spans = tuple(
                    s
                    for s in verified.projection.evidence
                    if s.start_char < parent.end_char and s.end_char > parent.start_char
                )
                if len(spans) > 512:
                    raise ValueError("父块证据片段超出读取预算")
                encoded_bytes = (
                    len(json.dumps(parent.text, ensure_ascii=False).encode()) + 4096
                )
                for span in spans:
                    # 标题可能反复出现在片段上下文；不能只限制父块文字而放大响应。
                    if sum(map(len, span.heading_path)) > 8192:
                        raise ValueError("证据标题上下文超出读取预算")
                    encoded_bytes += len(
                        json.dumps(asdict(span), ensure_ascii=False).encode()
                    )
                    if encoded_bytes > 1024 * 1024:
                        raise ValueError("证据响应超出1MiB预算")
                # S3回读后再判活；其后并发切换仍受普通读的快照语义约束。
                if (
                    await self._documents.get_active_revisions(
                        kb_id, [parent.key.source_id]
                    )
                    != active
                ):
                    raise EvidenceNotFound("证据不存在")
                return ParentEvidenceView(
                    kb_id,
                    parent.key.source_id,
                    parent.key.revision_id,
                    parent_id,
                    reference.bundle.canonical_sha256,
                    parent.start_char,
                    parent.end_char,
                    parent.text,
                    spans,
                )
        except (EvidenceNotFound, asyncio.CancelledError):
            raise
        except Exception as exc:
            # 对外统一脱敏；未知别名/错摘要/缺对象都不可回退未校验的父块正文。
            logger.warning(f"授权证据读取失败 reason={type(exc).__name__}")
            raise EvidenceUnavailable("证据暂不可用") from None
