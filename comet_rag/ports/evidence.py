from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol

from comet_rag.ports.document import ExtractedDocument
from comet_rag.ports.document_store import RevisionKey

MAX_EVIDENCE_PAGES = 512
BBox = tuple[float, float, float, float]


def evidence_digest(value: object) -> str:
    """证据身份只接受完整的小写SHA256，不把任意字符串当对象定位凭据。"""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("证据摘要格式无效")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """extraction只是提取来源，不是人工金标；bbox不定位逐字quote。"""

    pointer: str
    parent_pointer: str
    page_number: int
    block_type: str
    role: str
    category: Literal["extraction", "auxiliary"]
    start_char: int
    end_char: int
    heading_path: tuple[str, ...]
    bbox: BBox | None
    continues_prev: bool


@dataclass(frozen=True, slots=True)
class Interpretation:
    """模型读图结果独立保留，不进入可供原文引用的 canonical。"""

    pointer: str
    page_number: int
    block_type: str
    text: str
    bbox: BBox


@dataclass(frozen=True, slots=True)
class AssetFact:
    """只记来源存在性和摘要；不保留、解码或访问图片地址。"""

    pointer: str
    page_number: int
    locator_sha256: str
    locator_bytes: int


@dataclass(frozen=True, slots=True)
class StructuredProjection:
    """正文、页事实与证据账本的边界值；图像解读不作为逐字原文。"""

    source_sha256: str
    input_sha256: str
    canonical_sha256: str
    body_sha256: str
    document: ExtractedDocument
    evidence: tuple[EvidenceSpan, ...]
    interpretations: tuple[Interpretation, ...]
    assets: tuple[AssetFact, ...]

    def verify_quote(
        self,
        *,
        pointer: str,
        canonical_sha256: str,
        start_char: int,
        end_char: int,
        quote: str,
        allow_auxiliary: bool = False,
    ) -> None:
        """核对本轮区间及来源类别；不证明PDF字词正确或人工已验收。"""
        if canonical_sha256 != self.canonical_sha256:
            raise ValueError("证据不属于本轮 canonical")
        if type(allow_auxiliary) is not bool:
            raise ValueError("辅助文本引用许可必须是 bool")
        if type(start_char) is not int or type(end_char) is not int:
            raise ValueError("证据区间必须是整数")
        span = next((s for s in self.evidence if s.pointer == pointer), None)
        if span is None or (span.category == "auxiliary" and not allow_auxiliary):
            raise ValueError("该片段不具备请求的引用类别")
        if not span.start_char <= start_char < end_char <= span.end_char:
            raise ValueError("证据不得跨原生片段或页面")
        if self.document.markdown[start_char:end_char] != quote:
            raise ValueError("quote 不等于 canonical 区间")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """可放进有界恢复意图的小引用；摘要由调用方持久化，不由待读manifest自证。"""

    key: RevisionKey
    source_sha256: str
    pages: int
    canonical_sha256: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, RevisionKey):
            raise ValueError("证据引用需要revision身份")
        for value in (self.source_sha256, self.canonical_sha256, self.manifest_sha256):
            evidence_digest(value)
        if type(self.pages) is not int or not 0 < self.pages <= MAX_EVIDENCE_PAGES:
            raise ValueError("证据引用物理页数无效")

    @property
    def prefix(self) -> str:
        # 不把来源路径或未转义的业务标识当作对象路径。
        parts = (self.key.kb_id, self.key.source_id, self.key.revision_id)
        identity = "/".join(sha256(p.encode()).hexdigest() for p in parts)
        return f"evidence/v1/{identity}/{self.manifest_sha256}"

    def payload(self) -> dict[str, object]:
        """小引用只保存身份与摘要，不含源正文、端点或凭据。"""
        return {
            "schema_version": 1,
            "key": {
                "kb_id": self.key.kb_id,
                "source_id": self.key.source_id,
                "revision_id": self.key.revision_id,
            },
            "source_sha256": self.source_sha256,
            "pages": self.pages,
            "canonical_sha256": self.canonical_sha256,
            "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> EvidenceReference:
        """严格恢复受信持久化引用，拒绝bool页号、额外字段与无效摘要。"""
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "key",
            "source_sha256",
            "pages",
            "canonical_sha256",
            "manifest_sha256",
        }:
            raise ValueError("证据引用字段不匹配")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("证据引用版本不匹配")
        key = value["key"]
        if (
            not isinstance(key, dict)
            or set(key) != {"kb_id", "source_id", "revision_id"}
            or not all(isinstance(v, str) for v in key.values())
        ):
            raise ValueError("证据revision身份不匹配")
        return cls(
            RevisionKey(key["kb_id"], key["source_id"], key["revision_id"]),
            evidence_digest(value["source_sha256"]),
            value["pages"],
            evidence_digest(value["canonical_sha256"]),
            evidence_digest(value["manifest_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    reference: EvidenceReference
    manifest: bytes
    native: bytes
    ledger: bytes


class AsyncEvidenceStore(Protocol):
    """已验证原生字节及账本的不可变发布／有界回读；不承诺远端任务幂等。"""

    async def publish(self, bundle: EvidenceBundle) -> StructuredProjection:
        """同一引用异字节必须拒绝；提交不确定不能自动重写或删除。"""
        ...

    async def read(self, reference: EvidenceReference) -> StructuredProjection:
        """仅按可信小引用读取固定对象，完整校验后返回。"""
        ...
