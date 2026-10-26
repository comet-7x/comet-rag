from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from comet_rag.engines.documents.normalization.markdown import (
    MarkdownDocumentNormalizer,
)
from comet_rag.ports.document import ExtractedDocument, ExtractedPage
from evals.retrieval.data import (
    Corpus,
    Dataset,
    Judgments,
    Source,
    StrictModel,
    Text,
    locate_evidence,
    read_json,
    validate_sources,
    write_json,
)

DATA = Path(__file__).parent
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PDFPage(StrictModel):
    page_number: Annotated[int, Field(ge=1)]
    text: Text


class PDFSample(StrictModel):
    source: Source
    url: Text
    pdf_sha256: Digest
    pdf_bytes: Annotated[int, Field(gt=0)]
    total_pages: Annotated[int, Field(gt=0)]
    pages: Annotated[list[PDFPage], Field(min_length=1)]


class PDFSnapshot(StrictModel):
    version: Literal[1]
    extractor: Text
    label_author: Text
    samples: Annotated[list[PDFSample], Field(min_length=1)]


def load(data: Path = DATA) -> Dataset:
    """快照来自真实 PDF 的独立参考提取；不冒充 MinerU 的生产解析结果。"""
    snapshot = read_json(data / "pages.json", PDFSnapshot)
    labels = read_json(data / "judgments.json", Judgments)
    return from_snapshot(snapshot, labels)


def from_snapshot(snapshot: PDFSnapshot, labels: Judgments) -> Dataset:
    """复用同一物理页/标签校验；生产解析或来源身份由上游冻结工件证明。"""
    corpus = Corpus(version=1, documents=[s.source for s in snapshot.samples])
    validate_sources(corpus)
    documents = {}
    normalizer = MarkdownDocumentNormalizer()
    for sample in snapshot.samples:
        if sample.source.format != "pdf" or sample.source.sections:
            raise ValueError("真实页样本必须声明 pdf，不能混入生成式 sections")
        pages = tuple(ExtractedPage(p.page_number, p.text) for p in sample.pages)
        if any(page.page_number > sample.total_pages for page in pages):
            raise ValueError("物理页号超出原 PDF")
        extracted = ExtractedDocument(
            "\n\n".join(p.markdown for p in pages), pages=pages
        )
        document = normalizer.normalize(extracted)
        if len(document.blocks) != len(pages) or any(
            b.kind != "page" for b in document.blocks
        ):
            raise ValueError("参考快照未保持真实页事实")
        documents[sample.source.id] = document
    spans = locate_evidence(corpus, labels, documents)
    for evidence in labels.evidence:
        start, end = spans[evidence.id]
        if not any(
            b.start_char <= start < end <= b.end_char
            for b in documents[evidence.document_id].blocks
        ):
            raise ValueError("每份证据必须完整位于一页；跨页答案应分列多个原文证据")
    # 物理页身份也参与指纹；正文相同而页号变化不能混评。
    payload = {
        "snapshot": snapshot.model_dump(),
        "judgments": labels.model_dump(),
        "normalized": {k: v.markdown for k, v in documents.items()},
        "spans": spans,
    }
    fingerprint = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return Dataset(corpus, labels, documents, spans, fingerprint)


def blind_packet(output: Path, *, data: Path = DATA) -> None:
    """不导出作者答案、relevance、no-answer 标签或模型结果；人工验收保持 pending。"""
    dataset = load(data)
    snapshot = read_json(data / "pages.json", PDFSnapshot)
    output.mkdir(parents=True, exist_ok=False)
    questions = [
        {"query_id": q.id, "kb_id": q.kb_id, "question": q.text}
        for q in dataset.judgments.queries
    ]
    write_json(output / "questions.json", questions)
    write_json(output / "sources.json", snapshot.model_dump())
    write_json(
        output / "review.json",
        {
            "dataset_sha256": dataset.fingerprint,
            "status": "pending",
            "reviewer": "",
            "decisions": [
                {
                    "query_id": q.id,
                    "decision": "unreviewed",
                    "relevance": {},
                    "evidence": [],
                    "notes": "",
                }
                for q in dataset.judgments.queries
            ],
        },
    )
    (output / "README.md").write_text(
        "# 真实 PDF 盲审包\n\n仅含问题和来源/页面，不含作者拟定答案或检索排名。\n"
        "请由未参与作者标注的复核人阅读原 PDF，填写每题相关文档等级、物理页码、原文 quote、歧义与无答案依据。\n"
        "逐题填写 decision=answerable/unanswerable/uncertain；unreviewed 或空 relevance 不能自动视为已确认无答案。\n"
        "需要逐项裁决与作者标签的分歧后再冻结新金标。仅填写 reviewer 名称不能证明人的独立性。\n",
        encoding="utf-8",
    )
