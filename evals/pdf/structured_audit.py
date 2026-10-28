from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer
from evals.pdf.inspect import table_integrity
from evals.pdf.parse import page_summary
from evals.pdf.structured import (
    DEFAULT_LIMITS,
    POLICY,
    project_structured,
    projection_payload,
)


def read_limited(path: Path, limit: int) -> bytes:
    """文件和将来的流式HTTP都应先限字节；不先无界读取再检查。"""
    if path.stat().st_size > limit:
        raise ValueError("输入文件超出 wire 字节预算")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("输入文件超出 wire 字节预算")
    return raw


def audit(
    path: Path,
    source_lock: Path,
    sample_id: str,
    output: Path,
) -> dict[str, object]:
    """只审计已有真实产物，不连接模型，也不把实验账本写进生产存储。"""
    if output.exists():
        raise ValueError("输出目录必须是新目录")
    lock = json.loads(read_limited(source_lock, 1024 * 1024))
    samples = [s for s in lock["samples"] if s["id"] == sample_id]
    if len(samples) != 1:
        raise ValueError("来源锁必须唯一列出所选 PDF")
    sample = samples[0]
    source = Path(sample["path"])
    with source.open("rb") as stream:
        digest = sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if (
        source.stat().st_size != sample["bytes"]
        or digest.hexdigest() != sample["sha256"]
    ):
        raise ValueError("源 PDF 字节不属于本轮来源锁")
    raw = read_limited(path, DEFAULT_LIMITS.wire_bytes)
    result = project_structured(
        raw,
        source_sha256=sample["sha256"],
        expected_pages=sample["pages"],
    )
    normalized = MarkdownDocumentNormalizer().normalize(result.document)
    pages = page_summary(result.document, sample["pages"])
    for span in result.evidence:
        result.verify_quote(
            pointer=span.pointer,
            canonical_sha256=result.canonical_sha256,
            start_char=span.start_char,
            end_char=span.end_char,
            quote=result.document.markdown[span.start_char : span.end_char],
            allow_auxiliary=True,
        )
        if not any(
            b.page_number == span.page_number
            and b.start_char <= span.start_char < span.end_char <= b.end_char
            for b in normalized.blocks
        ):
            raise ValueError("证据片段跨越源物理页")
    ledger = projection_payload(result)
    summary: dict[str, object] = {
        "scope": "offline structured mapping; prior real parse; not production or independent quality acceptance",
        "sample": sample_id,
        "policy": POLICY,
        "source_sha256": result.source_sha256,
        "input_sha256": result.input_sha256,
        "canonical_sha256": result.canonical_sha256,
        "body_sha256": result.body_sha256,
        "wire_bytes": len(raw),
        "canonical_bytes": len(result.document.markdown.encode()),
        "limits": asdict(DEFAULT_LIMITS),
        "pages": pages,
        "evidence_units": len(result.evidence),
        "evidence_categories": dict(Counter(s.category for s in result.evidence)),
        "source_block_types": dict(
            Counter(s.block_type for s in result.evidence if s.role == "body")
        ),
        "heading_units": sum(
            s.block_type in {"doc_title", "paragraph_title"} for s in result.evidence
        ),
        "interpretation_units": len(result.interpretations),
        "interpretation_bytes": sum(
            len(i.text.encode()) for i in result.interpretations
        ),
        "asset_locators": len(result.assets),
        "asset_locator_bytes": sum(a.locator_bytes for a in result.assets),
        "legacy_page_blocks_have_heading_paths": any(
            b.heading_path for b in normalized.blocks
        ),
        "table_body_units": sum(
            s.block_type == "table" and s.role == "body" for s in result.evidence
        ),
        "html_table_diagnostics": table_integrity(normalized),
    }
    # 全部核验后才建目录，损坏输入不会旁留貌似成功的summary。
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("projection.json", ledger), ("summary.json", summary)):
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="离线评估 MinerU4 structured_content 的新正文及证据账本"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    summary = audit(args.input, args.source_lock, args.sample, args.out)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "sample",
                    "wire_bytes",
                    "canonical_bytes",
                    "evidence_units",
                    "interpretation_units",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
