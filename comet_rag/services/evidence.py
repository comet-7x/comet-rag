from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256

from comet_rag.engines.indexing import HierarchyParent
from comet_rag.ports.document_store import RevisionKey
from comet_rag.ports.evidence import (
    AsyncEvidenceStore,
    EvidenceReference,
    StructuredProjection,
)

PARENT_EVIDENCE_KEY = "evidence_reference"


@dataclass(frozen=True, slots=True)
class ParentEvidenceReference:
    """固定存储别名与包身份；不能持久化任意URL来控制后续读取目标。"""

    storage_id: str
    bundle: EvidenceReference

    def __post_init__(self) -> None:
        if not isinstance(self.storage_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", self.storage_id
        ):
            raise ValueError("证据存储身份必须是固定别名")
        if not isinstance(self.bundle, EvidenceReference):
            raise TypeError("父块证据需要经过校验的包引用")

    def payload(self) -> dict[str, object]:
        return {"storage_id": self.storage_id, "bundle": self.bundle.payload()}

    @classmethod
    def from_payload(cls, value: object) -> ParentEvidenceReference:
        if not isinstance(value, dict) or set(value) != {"storage_id", "bundle"}:
            raise ValueError("父块证据引用字段无效")
        return cls(value["storage_id"], EvidenceReference.from_payload(value["bundle"]))


@dataclass(frozen=True, slots=True)
class VerifiedEvidence:
    """应用内部的完整回读结果，不从请求metadata反序列化为可信证据。"""

    reference: ParentEvidenceReference
    projection: StructuredProjection

    def __post_init__(self) -> None:
        ref, result = self.reference.bundle, self.projection
        if (
            ref.source_sha256 != result.source_sha256
            or ref.canonical_sha256 != result.canonical_sha256
            or ref.pages != len(result.document.pages)
            or result.body_sha256
            != sha256(result.document.markdown.encode()).hexdigest()
        ):
            raise ValueError("回读证据与持久化引用不一致")

    def parent_metadata(
        self, key: RevisionKey, parents: Sequence[HierarchyParent]
    ) -> dict[str, object]:
        """索引前核对revision与每个父块原文区间，不给错正文附上可信包引用。"""
        if self.reference.bundle.key != key:
            raise ValueError("父块证据属于其他revision")
        if not parents:
            raise ValueError("没有父块时不能悄悄丢弃证据引用")
        text = self.projection.document.markdown
        for parent in parents:
            if (
                type(parent.start_char) is not int
                or type(parent.end_char) is not int
                or not 0 <= parent.start_char < parent.end_char <= len(text)
                or text[parent.start_char : parent.end_char] != parent.text
            ):
                raise ValueError("父块不对应证据正文区间")
        payload = self.reference.payload()
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 2048:
            raise ValueError("父块证据引用超出2KiB预算")
        return {PARENT_EVIDENCE_KEY: payload}


async def read_evidence(
    stores: Mapping[str, AsyncEvidenceStore], reference: ParentEvidenceReference
) -> VerifiedEvidence:
    """按持久化别名精确选择；未知身份不回退默认端点，也不解析任意URL。"""
    try:
        store = stores[reference.storage_id]
    except KeyError:
        raise ValueError("证据存储身份未配置") from None
    return VerifiedEvidence(reference, await store.read(reference.bundle))
