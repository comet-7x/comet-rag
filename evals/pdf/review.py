from __future__ import annotations

import argparse
import json
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import Field, JsonValue, RootModel

from evals.pdf.artifacts import FROZEN, blind_packet
from evals.pdf.data import DATA, Digest
from evals.retrieval.data import (
    Name,
    StrictModel,
    parse_json,
    read_json,
    write_json,
)

Nonblank = Annotated[str, Field(pattern=r"\S")]
PositiveInt = Annotated[int, Field(gt=0)]


class JsonDocument(RootModel[JsonValue]):
    pass


class Fragment(StrictModel):
    document_id: Name
    page_number: PositiveInt
    start_char: Annotated[int, Field(ge=0)]
    end_char: PositiveInt
    quote: Nonblank
    role: Literal["passage", "table_header", "table_row"]


class Decision(StrictModel):
    query_id: Name
    decision: Literal["answerable", "unanswerable", "uncertain"]
    relevance: dict[Name, Annotated[int, Field(ge=0, le=2)]]
    evidence: list[Fragment]
    notes: str


class Review(StrictModel):
    review_scope_sha256: Digest
    full_canonical_sha256: Digest
    status: Literal["submitted"]
    reviewer: Nonblank
    reviewed_at: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    independence_basis: Nonblank
    decisions: Annotated[list[Decision], Field(min_length=1)]


def _canonical_json(path: Path) -> str:
    # 比较JSON字面类型，避免dict相等把False/0或True/1混为同一页事实。
    value = read_json(path, JsonDocument).root
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def validate_review(
    packet: Path,
    response: Path,
    *,
    frozen: Path = FROZEN,
    data: Path = DATA,
) -> dict[str, object]:
    """验证复核材料的结构与原文事实；不认证人的独立性，不自动裁决或生成金标。"""
    raw = response.read_bytes()
    review = parse_json(raw, Review)
    date.fromisoformat(review.reviewed_at)
    with TemporaryDirectory(prefix="comet-review-inputs-") as folder:
        expected = Path(folder) / "packet"
        blind_packet(frozen, expected, data=data)
        for name in ("questions.json", "sources.json"):
            if _canonical_json(packet / name) != _canonical_json(expected / name):
                raise ValueError("盲审问题或来源已偏离冻结输入")
        template = json.loads((expected / "review.json").read_text())
        sources = json.loads((expected / "sources.json").read_text())["sources"]
        questions = json.loads((expected / "questions.json").read_text())
    if (
        review.review_scope_sha256 != template["review_scope_sha256"]
        or review.full_canonical_sha256 != template["full_canonical_sha256"]
    ):
        raise ValueError("复核表不属于此 canonical 和问题页域")
    decisions = {d.query_id: d for d in review.decisions}
    if len(decisions) != len(review.decisions) or set(decisions) != {
        q["query_id"] for q in questions
    }:
        raise ValueError("复核必须精确覆盖全部问题，不能缺失、重复或混入未知问题")
    source_map = {s["document_id"]: s for s in sources}
    unresolved = []
    for question in questions:
        decision = decisions[question["query_id"]]
        allowed = {
            name
            for name, source in source_map.items()
            if source["kb_id"] == question["kb_id"]
        }
        if set(decision.relevance) - allowed:
            raise ValueError("相关度包含未知来源或跨知识库文档")
        positive = {name for name, grade in decision.relevance.items() if grade > 0}
        if decision.decision == "answerable" and (
            not positive or not decision.evidence
        ):
            raise ValueError("可回答判断必须有相关来源及逐字证据")
        if decision.decision == "unanswerable" and (
            positive or decision.evidence or not decision.notes.strip()
        ):
            raise ValueError("无答案判断必须说明检查范围且不能带正相关或答案证据")
        if decision.decision == "uncertain":
            if not decision.notes.strip():
                raise ValueError("不确定判断必须说明待裁决问题")
            unresolved.append(decision.query_id)
        if len(decision.evidence) > 1 and not decision.notes.strip():
            raise ValueError("多片段证据须说明联合需求，不能当作一个连续片段")
        seen = set()
        for fragment in decision.evidence:
            if fragment.document_id not in positive:
                raise ValueError("证据只能引用同知识库的正相关来源")
            source = source_map[fragment.document_id]
            pages = {p["page_number"]: p for p in source["pages"]}
            page = pages.get(fragment.page_number)
            if page is None or not (
                page["start_char"]
                <= fragment.start_char
                < fragment.end_char
                <= page["end_char"]
            ):
                raise ValueError("证据必须位于问题页域的一页内，跨页应分列")
            start = fragment.start_char - page["start_char"]
            end = fragment.end_char - page["start_char"]
            if page["markdown"][start:end] != fragment.quote:
                raise ValueError("证据 quote 与冻结原文区间不一致")
            identity = (fragment.document_id, fragment.start_char, fragment.end_char)
            if identity in seen:
                raise ValueError("同一原文片段不能重复计作多份证据")
            seen.add(identity)
    return {
        "scope": "structural and literal evidence validation only; not independent adjudication",
        "review_scope_sha256": review.review_scope_sha256,
        "full_canonical_sha256": review.full_canonical_sha256,
        "response_sha256": sha256(raw).hexdigest(),
        "questions": len(decisions),
        "unresolved_queries": unresolved,
        "ready_for_adjudication": not unresolved,
        "independent_review": "unverified",
        "quality_acceptance": "pending",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="严格核验原生PDF复核表；不自动批准标签"
    )
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--native-dir", type=Path, default=FROZEN)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = validate_review(args.packet, args.response, frozen=args.native_dir)
    args.out.mkdir(parents=True, exist_ok=False)
    write_json(args.out / "validation.json", result)


if __name__ == "__main__":
    main()
