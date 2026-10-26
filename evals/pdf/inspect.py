from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from comet_rag.engines.chunkers import RecursiveChunker
from comet_rag.engines.chunkers.strategies import PageChunkingStrategy
from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.ports import ExtractedDocument, ExtractedPage, NormalizedDocument
from evals.pdf.data import DATA, Digest, PDFSnapshot, load
from evals.pdf.parse import page_summary
from evals.pdf.tables import tables
from evals.retrieval.data import (
    Name,
    StrictModel,
    Text,
    quote_locations,
    read_json,
    write_json,
)

PositiveInt = Annotated[int, Field(gt=0)]


class NativeProvider(StrictModel):
    provider: Literal["mineru"]
    backend: Text
    version: Text
    protocol_version: Annotated[int, Field(ge=2, le=2)]


class NativeSample(StrictModel):
    source_id: Name
    source_sha256: Digest
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    provider: NativeProvider
    page_status: Literal["verified"]
    page_numbers: Annotated[list[PositiveInt], Field(min_length=1)]
    normalized_empty_page_numbers: list[PositiveInt] = Field(default_factory=list)
    normalized_sha256: Digest
    normalized_characters: PositiveInt
    block_count: PositiveInt


class NativeReport(StrictModel):
    scope: Text
    snapshot_sha256: Digest
    samples: Annotated[list[NativeSample], Field(min_length=1)]


class NativePage(StrictModel):
    page_number: PositiveInt
    markdown: str


class NativeOutput(StrictModel):
    markdown: Text
    pages: Annotated[list[NativePage], Field(min_length=1)]


def load_native(
    directory: Path, *, data: Path = DATA
) -> tuple[dict[str, NormalizedDocument], str]:
    """检查来源锁和真实解析产物的一致性；不声称由可编辑报告证明上游身份或权重。"""
    snapshot = read_json(data / "pages.json", PDFSnapshot)
    report = read_json(directory / "report.json", NativeReport)
    if report.snapshot_sha256 != sha256((data / "pages.json").read_bytes()).hexdigest():
        raise ValueError("原生报告不属于这份 PDF 来源锁")
    samples = {sample.source_id: sample for sample in report.samples}
    if len(samples) != len(report.samples) or set(samples) != {
        s.source.id for s in snapshot.samples
    }:
        raise ValueError("原生样本清单缺失、重复或包含未知来源")
    documents = {}
    for source in snapshot.samples:
        name = source.source.id
        if not name.isalnum():
            raise ValueError("PDF 来源 id 不可用于文件名")
        sample = samples[name]
        if sample.source_sha256 != source.pdf_sha256:
            raise ValueError("原生解析的 PDF 字节身份与来源锁不一致")
        output = read_json(directory / f"{name}.json", NativeOutput)
        document = ExtractedDocument(
            output.markdown,
            pages=tuple(ExtractedPage(p.page_number, p.markdown) for p in output.pages),
        )
        summary = page_summary(document, source.total_pages)
        if summary["page_numbers"] != list(range(1, source.total_pages + 1)):
            raise ValueError("原生产物要求完整物理页，不能遗漏任意一页")
        for key in (
            "page_status",
            "page_numbers",
            "normalized_empty_page_numbers",
            "normalized_sha256",
            "normalized_characters",
            "block_count",
        ):
            if summary[key] != getattr(sample, key):
                raise ValueError(f"原生正文/页事实与报告不一致：{name}/{key}")
        if not {page.page_number for page in source.pages} <= set(sample.page_numbers):
            raise ValueError("参考问题范围内的物理页在原生结果中缺失")
        documents[name] = MarkdownDocumentNormalizer().normalize(document)
    # 只让内容/来源/页号参与 canonical 身份，不把 wall time 当成语义变化。
    identity = {
        name: (
            samples[name].source_sha256,
            samples[name].normalized_sha256,
            samples[name].page_numbers,
        )
        for name in sorted(samples)
    }
    fingerprint = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return documents, fingerprint


def evidence_alignment(
    documents: dict[str, NormalizedDocument], *, data: Path = DATA
) -> list[dict[str, object]]:
    reference = load(data)
    result = []
    for evidence in reference.judgments.evidence:
        ref_start, ref_end = reference.spans[evidence.id]
        expected = next(
            b.page_number
            for b in reference.documents[evidence.document_id].blocks
            if b.start_char <= ref_start < ref_end <= b.end_char
        )
        document = documents[evidence.document_id]
        locations = quote_locations(document.markdown, evidence.quote)
        aligned = False
        if len(locations) == 1:
            start = locations[0]
            aligned = any(
                b.page_number == expected
                and b.start_char <= start < start + len(evidence.quote) <= b.end_char
                for b in document.blocks
            )
        result.append(
            {
                "evidence_id": evidence.id,
                "document_id": evidence.document_id,
                "expected_physical_page": expected,
                "match_count_lower_bound": len(locations),
                "alignment": "exact_on_expected_page" if aligned else "needs_review",
                "native_span": [locations[0], locations[0] + len(evidence.quote)]
                if aligned
                else None,
                "independent_review": "pending",
            }
        )
    return result


def _covered(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(a <= start < end <= b for a, b in spans)


def table_integrity(
    document: NormalizedDocument, *, separators: tuple[str, ...] | None = None
) -> list[dict[str, object]]:
    """只描述实际返回单元的覆盖，不把被分开返回的碎片拼成完整证据。"""
    drafts = PageChunkingStrategy(
        RecursiveChunker(800, 80, separators=separators)
    ).split(document)
    hierarchy = HierarchyBuilder(
        RecursiveChunker(1600, separators=separators),
        RecursiveChunker(800, 80, separators=separators),
    ).split(document)
    result = []
    for page in document.blocks:
        if page.kind != "page":
            raise ValueError("原生表格审计要求真实 page blocks")
        text = document.markdown[page.start_char : page.end_char]
        for table in tables(text):
            start, end = page.start_char + table.start, page.start_char + table.end
            rows = [(page.start_char + a, page.start_char + b) for a, b in table.rows]
            record: dict[str, object] = {
                "physical_page": page.page_number,
                "span": [start, end],
                "table_characters": end - start,
                "rows": len(rows),
                "rows_over_child_budget": sum(b - a > 800 for a, b in rows),
                "rows_over_parent_budget": sum(b - a > 1600 for a, b in rows),
            }
            for name, chunks in (
                ("page_child", drafts),
                ("hierarchy_child", hierarchy.children),
                ("parent", hierarchy.parents),
            ):
                spans = [
                    (d.start_char, d.end_char)
                    for d in chunks
                    if d.start_char is not None and d.end_char is not None
                ]
                if len(spans) != len(chunks):
                    raise ValueError("表格单元审计要求可靠字符区间")
                record[name] = {
                    "whole_table": _covered(spans, start, end),
                    "whole_rows": sum(_covered(spans, a, b) for a, b in rows),
                }
            result.append(record)
    return result


def inspect(directory: Path, output: Path, *, data: Path = DATA) -> None:
    documents, fingerprint = load_native(directory, data=data)
    alignment = evidence_alignment(documents, data=data)
    table_report = {
        name: table_integrity(document) for name, document in documents.items()
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(
        output / "audit.json",
        {
            "scope": "native canonical consistency and table-unit diagnostics; not quality acceptance or a blind review packet",
            "native_canonical_sha256": fingerprint,
            "reference_dataset_sha256": load(data).fingerprint,
            "evidence_alignment": alignment,
            "tables": table_report,
            "settings": {
                "child_characters": 800,
                "overlap_characters": 80,
                "parent_characters": 1600,
            },
            "independent_review": "pending",
            "fixed_weights": "unknown",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="原生 PDF 证据对齐与完整表格/行诊断，不访问网络"
    )
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    inspect(args.native_dir, args.out)
