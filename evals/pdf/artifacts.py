from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from pydantic import Field

from comet_rag.ports import NormalizedDocument
from evals.pdf.data import DATA, Digest, PDFSnapshot
from evals.pdf.inspect import load_native
from evals.retrieval.data import (
    Corpus,
    Judgments,
    StrictModel,
    read_json,
    validate_sources,
    write_json,
)

ATTRIBUTION = (
    "Republished courtesy of the National Institute of Standards and Technology."
)
FROZEN = DATA / "native" / "20261025-b30"


class Manifest(StrictModel):
    version: Annotated[int, Field(ge=1, le=1)]
    canonical_sha256: Digest
    files: dict[str, Digest]
    attribution: Annotated[str, Field(pattern=r"\S")]


def _names(data: Path) -> list[str]:
    snapshot = read_json(data / "pages.json", PDFSnapshot)
    validate_sources(Corpus(version=1, documents=[s.source for s in snapshot.samples]))
    if any(not s.source.id.isalnum() for s in snapshot.samples):
        raise ValueError("PDF 来源 id 不可用于文件名")
    return ["report.json", *(f"{s.source.id}.json" for s in snapshot.samples)]


def freeze(
    directory: Path, output: Path, *, data: Path = DATA, attribution: str | None = None
) -> None:
    """校验后保存原始产物字节；manifest 最后落盘，部分写入不能成为有效快照。"""
    if attribution is None:
        if data.resolve() != DATA.resolve():
            raise ValueError("自定义 PDF 来源必须明确声明归属，不能沿用 NIST 署名")
        attribution = ATTRIBUTION
    if not attribution.strip():
        raise ValueError("PDF 归属声明不能为空")
    _, fingerprint = load_native(directory, data=data)
    files = {name: (directory / name).read_bytes() for name in _names(data)}
    # 先固定输入字节，再验证它们；防止边读边变导致保存的内容不是刚验证的内容。
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        (output / name).write_bytes(raw)
    _, stored = load_native(output, data=data)
    if stored != fingerprint:
        raise ValueError("冻结时原生 canonical 发生变化")
    manifest = Manifest(
        version=1,
        canonical_sha256=stored,
        files={name: sha256(raw).hexdigest() for name, raw in files.items()},
        attribution=attribution,
    )
    write_json(output / "manifest.json", manifest.model_dump())


def _read_frozen(
    directory: Path, *, data: Path
) -> tuple[dict[str, NormalizedDocument], Manifest]:
    """检查已提交快照的字节与语义身份；本地校验和不代替签名、权重或人的独立性。"""
    manifest = read_json(directory / "manifest.json", Manifest)
    if set(manifest.files) != set(_names(data)):
        raise ValueError("冻结产物清单必须精确覆盖所有来源和报告")
    for name, digest in manifest.files.items():
        if sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"冻结产物字节已变化：{name}")
    documents, fingerprint = load_native(directory, data=data)
    if fingerprint != manifest.canonical_sha256:
        raise ValueError("冻结产物 canonical 身份不一致")
    return documents, manifest


def load_frozen(
    directory: Path = FROZEN, *, data: Path = DATA
) -> tuple[dict[str, NormalizedDocument], str]:
    documents, manifest = _read_frozen(directory, data=data)
    return documents, manifest.canonical_sha256


def blind_packet(directory: Path, output: Path, *, data: Path = DATA) -> None:
    """只导出原问题的选定页；不读取旧 quote 定位、相关度或模型排名来生成答案。"""
    documents, manifest = _read_frozen(directory, data=data)
    fingerprint = manifest.canonical_sha256
    snapshot = read_json(data / "pages.json", PDFSnapshot)
    labels = read_json(data / "judgments.json", Judgments)
    questions = [
        {"query_id": q.id, "kb_id": q.kb_id, "question": q.text} for q in labels.queries
    ]
    if len({q.id for q in labels.queries}) != len(labels.queries):
        raise ValueError("问题 id 必须唯一")
    if {q.kb_id for q in labels.queries} - {s.source.kb_id for s in snapshot.samples}:
        raise ValueError("问题引用未知知识库")
    sources = []
    for sample in snapshot.samples:
        document = documents[sample.source.id]
        selected = {p.page_number for p in sample.pages}
        sources.append(
            {
                "document_id": sample.source.id,
                "kb_id": sample.source.kb_id,
                "title": sample.source.title,
                "url": sample.url,
                "pdf_sha256": sample.pdf_sha256,
                "total_pages": sample.total_pages,
                "pages": [
                    {
                        "page_number": b.page_number,
                        "start_char": b.start_char,
                        "end_char": b.end_char,
                        "markdown": document.markdown[b.start_char : b.end_char],
                    }
                    for b in document.blocks
                    if b.page_number in selected
                ],
            }
        )
    payload = {
        "full_canonical_sha256": fingerprint,
        "questions": questions,
        "sources": sources,
    }
    identity = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "questions.json", questions)
    write_json(
        output / "sources.json",
        {
            "attribution": manifest.attribution,
            "full_canonical_sha256": fingerprint,
            "sources": sources,
        },
    )
    write_json(
        output / "review.json",
        {
            "review_scope_sha256": identity,
            "full_canonical_sha256": fingerprint,
            "status": "pending",
            "reviewer": "",
            "reviewed_at": "",
            "independence_basis": "",
            "decisions": [
                {
                    "query_id": q.id,
                    "decision": "unreviewed",
                    "relevance": {},
                    "evidence": [],
                    "notes": "",
                }
                for q in labels.queries
            ],
        },
    )
    (output / "README.md").write_text(
        "# 原生 PDF 盲审包\n\n" + manifest.attribution + "\n\n"
        "仅依据 sources.json 的选定物理页作答；完整原 PDF 其他页不在问题范围。\n"
        "正文是冻结的 MinerU 结果，不保证 OCR 全保真；请对照原 PDF，记录解析遗漏和争议。\n"
        "逐题 decision=answerable/unanswerable/uncertain，空 relevance 不等于无答案。\n"
        "每个 evidence 写 document_id、page_number、start_char、end_char、quote、role。\n"
        "区间是 sources.json 所给完整规范正文的字符偏移，左闭右开。\n"
        "role 为 passage/table_header/table_row；表头与数据行、跨页答案分列片段，\n"
        "不得复制表头进原文或拼造连续 quote。notes 说明哪些片段需联合才能回答。\n"
        "填写 reviewer、reviewed_at 和 independence_basis 后，仍需裁决分歧；\n"
        "这些字段不认证人的独立性，工具不自动批准标签、模型或生产质量。\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="冻结完整原生 PDF 产物或生成选定页盲审包"
    )
    parser.add_argument("operation", choices=("freeze", "review"))
    parser.add_argument("--native-dir", type=Path, default=FROZEN)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "freeze":
        freeze(args.native_dir, args.out)
    else:
        blind_packet(args.native_dir, args.out)


if __name__ == "__main__":
    main()
