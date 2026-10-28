from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from comet_rag.engines.documents.mineru.structured import (
    DEFAULT_LIMITS,
    POLICY,
    StructuredProjection,
    decode_json,
    project_structured,
    projection_payload,
)
from comet_rag.infrastructure.evidence.files import read_limited
from comet_rag.ports.document_store import RevisionKey
from comet_rag.ports.evidence import (
    EvidenceBundle as EvidenceBundle,
)
from comet_rag.ports.evidence import (
    EvidenceReference as EvidenceReference,
)
from comet_rag.ports.evidence import (
    evidence_digest as digest,
)

MANIFEST_BYTES = 64 * 1024
LEDGER_BYTES = 16 * 1024 * 1024
_FILES = {
    "structured_content.json": DEFAULT_LIMITS.wire_bytes,
    "ledger.json": LEDGER_BYTES,
}


def _json(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    ).encode()


def build_bundle(
    raw: bytes, *, key: RevisionKey, source_sha256: str, pages: int
) -> EvidenceBundle:
    """构建后先持久化reference意图，再发布对象；发布失败不重新解析远端PDF。"""
    result = project_structured(raw, source_sha256=source_sha256, expected_pages=pages)
    ledger = _json(projection_payload(result))
    if len(ledger) > LEDGER_BYTES:
        raise ValueError("证据账本超出独立预算")
    manifest = _json(
        {
            "schema_version": 1,
            "policy": POLICY,
            "key": {
                "kb_id": key.kb_id,
                "source_id": key.source_id,
                "revision_id": key.revision_id,
            },
            "source_sha256": source_sha256,
            "pages": pages,
            "canonical_sha256": result.canonical_sha256,
            "files": {
                name: {"bytes": len(data), "sha256": sha256(data).hexdigest()}
                for name, data in (
                    ("structured_content.json", raw),
                    ("ledger.json", ledger),
                )
            },
        }
    )
    return EvidenceBundle(
        EvidenceReference(
            key,
            source_sha256,
            pages,
            result.canonical_sha256,
            sha256(manifest).hexdigest(),
        ),
        manifest,
        raw,
        ledger,
    )


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def publish_bundle(bundle: EvidenceBundle, output: Path) -> None:
    """本地对象协议原型；新目录、manifest最后写，不实现S3上传或原地覆盖。"""
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    _write(output / "structured_content.json", bundle.native)
    _write(output / "ledger.json", bundle.ledger)
    _write(output / "manifest.json", bundle.manifest)
    descriptor = os.open(output, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    size: int
    sha256: str


def manifest_files(
    manifest_raw: bytes, expected: EvidenceReference
) -> dict[str, EvidenceFile]:
    """本地与S3共用身份校验；远端声明先限长，再读取对应对象。"""
    if len(manifest_raw) > MANIFEST_BYTES:
        raise ValueError("证据manifest超出预算")
    if sha256(manifest_raw).hexdigest() != expected.manifest_sha256:
        raise ValueError("证据manifest不属于持久化引用")
    manifest = decode_json(manifest_raw)
    identity = expected.payload()
    del identity["manifest_sha256"]
    if _json({k: v for k, v in manifest.items() if k != "files"}) != _json(
        {**identity, "policy": POLICY}
    ):
        raise ValueError("证据manifest身份不匹配")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(_FILES):
        raise ValueError("证据manifest文件集合不匹配")
    facts: dict[str, EvidenceFile] = {}
    for name, limit in _FILES.items():
        entry = files[name]
        if (
            not isinstance(entry, dict)
            or set(entry) != {"bytes", "sha256"}
            or type(entry.get("bytes")) is not int
            or not 0 < entry["bytes"] <= limit
        ):
            raise ValueError("证据文件长度或摘要不符")
        facts[name] = EvidenceFile(entry["bytes"], digest(entry.get("sha256")))
    return facts


def validate_bundle_bytes(
    manifest_raw: bytes, payloads: dict[str, bytes], expected: EvidenceReference
) -> StructuredProjection:
    """字节和语义只有一个校验源；S3读取不另写宽松版账本解释器。"""
    facts = manifest_files(manifest_raw, expected)
    if set(payloads) != set(facts):
        raise ValueError("证据包文件不完整或多出文件")
    for name, fact in facts.items():
        raw = payloads[name]
        if len(raw) != fact.size or sha256(raw).hexdigest() != fact.sha256:
            raise ValueError("证据文件长度或摘要不符")
    result = project_structured(
        payloads["structured_content.json"],
        source_sha256=expected.source_sha256,
        expected_pages=expected.pages,
    )
    if result.canonical_sha256 != expected.canonical_sha256:
        raise ValueError("证据canonical不匹配")
    # 比较确定性编码，避免JSON的True==1让伪造页号/区间穿过深层比较。
    if _json(decode_json(payloads["ledger.json"])) != _json(projection_payload(result)):
        raise ValueError("持久化账本不等于原生重新映射结果")
    return result


def load_bundle(output: Path, expected: EvidenceReference) -> StructuredProjection:
    """从独立可信引用核验三份有界字节，重新映射对照账本，不信任可编辑hash自证。"""
    if {p.name for p in output.iterdir()} != {*_FILES, "manifest.json"}:
        raise ValueError("证据包文件不完整或多出文件")
    manifest_raw = read_limited(output / "manifest.json", MANIFEST_BYTES)
    facts = manifest_files(manifest_raw, expected)
    return validate_bundle_bytes(
        manifest_raw,
        {name: read_limited(output / name, fact.size) for name, fact in facts.items()},
        expected,
    )
