from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from evals.pdf.artifacts import FROZEN, load_frozen
from evals.pdf.data import DATA, Digest, PDFSnapshot, from_snapshot
from evals.pdf.review import Fragment
from evals.pdf.tables import tables
from evals.retrieval.data import Dataset, Judgments, Name, StrictModel, Text, read_json
from evals.retrieval.metrics import Run, score

CASES = DATA / "table_cases" / "cases.json"
ROW_SEPARATORS = ("</tr>", "\n\n", "\n", " ", "")


class TableCase(StrictModel):
    query_id: Name
    question: Text
    header: Fragment
    row: Fragment


class TableCases(StrictModel):
    version: Annotated[int, Field(ge=1, le=1)]
    label_status: Literal["author_provisional"]
    full_canonical_sha256: Digest
    cases: Annotated[list[TableCase], Field(min_length=1)]


def load_cases(
    *, frozen: Path = FROZEN, cases: Path = CASES, data: Path = DATA
) -> Dataset:
    """作者暂定表格题的局部探索；完整原文与选定页索引分别绑定，不借旧span。"""
    full, fingerprint = load_frozen(frozen, data=data)
    labels = read_json(cases, TableCases)
    if labels.full_canonical_sha256 != fingerprint:
        raise ValueError("表格标签不属于这轮原生canonical")
    snapshot = read_json(data / "pages.json", PDFSnapshot)
    samples = []
    for sample in snapshot.samples:
        document = full[sample.source.id]
        pages = [
            {
                "page_number": b.page_number,
                "text": document.markdown[b.start_char : b.end_char],
            }
            for b in document.blocks
            if b.page_number in {p.page_number for p in sample.pages}
        ]
        # revision描述这一份实际送入索引的页域，不能只绑定未变的整份PDF。
        identity = {
            "kb_id": sample.source.kb_id,
            "document_id": sample.source.id,
            "source_id": sample.source.source_id,
            "pdf_sha256": sample.pdf_sha256,
            "pages": pages,
        }
        revision = (
            "n_"
            + sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()[:32]
        )
        samples.append(
            {
                **sample.model_dump(),
                "source": {**sample.source.model_dump(), "revision": revision},
                "pages": pages,
            }
        )
    evidence, queries = [], []
    sources = {s.source.id: s.source for s in snapshot.samples}
    for case in labels.cases:
        header, row = case.header, case.row
        if (
            header.document_id != row.document_id
            or header.page_number != row.page_number
        ):
            raise ValueError("本次表格单元须由同一页同一表的表头和数据行组成")
        if header.role != "table_header" or row.role != "table_row":
            raise ValueError("表格片段role必须明确区分表头和数据行")
        document = full.get(header.document_id)
        if document is None:
            raise ValueError("未知表格来源")
        page = next(
            (b for b in document.blocks if b.page_number == header.page_number), None
        )
        if page is None:
            raise ValueError("未知表格物理页")
        for fragment in (header, row):
            if (
                not page.start_char
                <= fragment.start_char
                < fragment.end_char
                <= page.end_char
                or document.markdown[fragment.start_char : fragment.end_char]
                != fragment.quote
            ):
                raise ValueError("表格证据必须是原生页内的逐字区间")
        lexical = tables(document.markdown[page.start_char : page.end_char])
        hspan = (header.start_char - page.start_char, header.end_char - page.start_char)
        rspan = (row.start_char - page.start_char, row.end_char - page.start_char)
        if not any(t.rows[0] == hspan and rspan in t.rows[1:] for t in lexical):
            raise ValueError("表头和数据行必须对应同一真实HTML表的显式行")
        ids = []
        for role, fragment in (("header", header), ("row", row)):
            eid = f"{case.query_id}-{role}"
            ids.append(eid)
            evidence.append(
                {
                    "id": eid,
                    "document_id": fragment.document_id,
                    "quote": fragment.quote,
                }
            )
        source = sources[header.document_id]
        queries.append(
            {
                "id": case.query_id,
                "kb_id": source.kb_id,
                "text": case.question,
                "category": "table",
                "relevance": {header.document_id: 2},
                "evidence": ids,
            }
        )
    derived = PDFSnapshot.model_validate(
        {
            "version": 1,
            "extractor": "Frozen MinerU selected normalized pages; full canonical "
            + fingerprint,
            "label_author": "batch33-author provisional; independent human review pending",
            "samples": samples,
        }
    )
    dataset = from_snapshot(
        derived,
        Judgments.model_validate(
            {"version": 1, "evidence": evidence, "queries": queries}
        ),
    )
    # 旧来源中的选定页仍是问题域，不能因为完整文件可用而将其他75页混入索引。
    for case in labels.cases:
        header = case.header
        if not any(
            b.page_number == header.page_number
            for b in dataset.documents[header.document_id].blocks
        ):
            raise ValueError("表格问题超出已声明的选定页域")
    return dataset


def joint_report(dataset: Dataset, run: Run) -> dict[str, object]:
    """先沿用严格评分验原文；额外要求同一返回单元同时包含表头与数据行。"""
    report = score(dataset, run)
    queries = {q.id: q for q in dataset.judgments.queries}
    evidence = {e.id: e for e in dataset.judgments.evidence}
    rows = []
    for result in run.results:
        query = queries[result.query_id]
        if query.evidence != [f"{query.id}-header", f"{query.id}-row"]:
            raise ValueError("联合表格指标要求明确的表头和数据行标签")
        parts = [
            (evidence[eid].document_id, *dataset.spans[eid]) for eid in query.evidence
        ]
        row = {
            "query_id": query.id,
            "minimum_contiguous_characters": max(b for _, a, b in parts)
            - min(a for _, a, b in parts),
        }
        for k in (1, 3, 5):
            row[f"one_hit_joint@{k}"] = any(
                all(
                    hit.document_id == doc and hit.start <= start < end <= hit.end
                    for doc, start, end in parts
                )
                for hit in result.hits[:k]
            )
        rows.append(row)
    report["joint_units"] = rows
    report["quality_acceptance"] = "pending; author-provisional table cases only"
    return report
