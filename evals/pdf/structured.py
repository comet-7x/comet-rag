from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Literal, cast

from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer
from comet_rag.ports import ExtractedDocument, ExtractedPage

POLICY = "mineru-4.0.12-structured-source-v1"
_TEXT = {"text", "list", "ref_text", "page_footnote"}
_AUXILIARY = {"header", "footer", "page_number"}
_TITLES = {"doc_title", "paragraph_title"}
_VISUAL = {"image", "chart", "table"}
_FIELDS = {
    "type",
    "content",
    "bbox",
    "level",
    "sub_type",
    "continues_prev",
    "captions",
    "footnotes",
    "image_source",
    "anchor",
}
BBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class StructuredLimits:
    """独立实验预算；不改服务配置，也不承诺 JSON 对象的峰值 RSS。"""

    wire_bytes: int = 32 * 1024 * 1024
    text_bytes: int = 8 * 1024 * 1024
    interpretation_bytes: int = 2 * 1024 * 1024
    block_bytes: int = 1024 * 1024
    pages: int = 512
    blocks: int = 20_000
    annotations_per_group: int = 128
    evidence_units: int = 50_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")


DEFAULT_LIMITS = StructuredLimits()


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
    """只供离线评估的正文及证据账本，不是新的生产 Port 或持久化字段。"""

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


def projection_payload(result: StructuredProjection) -> dict[str, object]:
    """采集后审计与证据包共用一个账本形态，不在存储层重新定义证据语义。"""
    return {
        "policy": POLICY,
        "source_sha256": result.source_sha256,
        "input_sha256": result.input_sha256,
        "canonical_sha256": result.canonical_sha256,
        "body_sha256": result.body_sha256,
        "markdown": result.document.markdown,
        "pages": [
            {"page_number": p.page_number, "markdown": p.markdown}
            for p in result.document.pages
        ],
        "evidence": [asdict(s) for s in result.evidence],
        "interpretations": [asdict(s) for s in result.interpretations],
        "assets": [asdict(s) for s in result.assets],
    }


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("需要 JSON object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("需要 JSON array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("需要 JSON string")
    return value


def _bbox(value: object) -> BBox:
    coordinates = _array(value)
    if len(coordinates) != 4:
        raise ValueError("bbox 必须是有限的归一化坐标")
    for value in coordinates:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("bbox 必须是有限的归一化坐标")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("bbox 必须是有限的归一化坐标")
    a, b, c, d = (float(cast(float, v)) for v in coordinates)
    if a >= c or b >= d:
        raise ValueError("bbox 不能是空或反向矩形")
    return a, b, c, d


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复键")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON 不允许非有限数字")
    return number


def _invalid_constant(value: str) -> object:
    raise ValueError(f"JSON 不允许 {value}")


def decode_json(raw: bytes) -> dict[str, object]:
    """有限读取后的严格JSON入口，采集控制响应与离线产物共用。"""
    try:
        return _object(
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_float=_finite_float,
                parse_constant=_invalid_constant,
            )
        )
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("JSON编码或嵌套无效") from exc


def project_structured(
    raw: bytes,
    *,
    source_sha256: str,
    expected_pages: int,
    limits: StructuredLimits = DEFAULT_LIMITS,
) -> StructuredProjection:
    """复用上游已经渲染的块内容；只定义实验拼接、定位和证据类别。"""
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("需要独立核验的源 PDF SHA-256")
    if type(expected_pages) is not int or not 0 < expected_pages <= limits.pages:
        raise ValueError("源物理页数超出实验预算")
    if len(raw) > limits.wire_bytes:
        raise ValueError("structured_content 超出 wire 字节预算")
    payload = decode_json(raw)
    if set(payload) - {"pages", "metadata", "extensions", "is_full_document"}:
        raise ValueError("不是已核验的 structured_content 形态")
    metadata = _object(payload.get("metadata"))
    producer = _object(metadata.get("producer"))
    source = _object(metadata.get("document"))
    if (
        producer != {"name": "mineru", "version": "4.0.12"}
        or metadata.get("file_suffix") != "pdf"
        or payload.get("is_full_document") is not True
        or type(source.get("page_count")) is not int
        or source["page_count"] != expected_pages
        or source.get("page_count_kind") != "physical"
    ):
        raise ValueError("生产者或完整 PDF 元数据不符")
    pages = _array(payload.get("pages"))
    if len(pages) != expected_pages:
        raise ValueError("缺失或多出物理页")

    normalizer = MarkdownDocumentNormalizer()
    evidence: list[EvidenceSpan] = []
    interpretations: list[Interpretation] = []
    assets: list[AssetFact] = []
    extracted_pages: list[ExtractedPage] = []
    headings: list[tuple[int, str]] = []
    cursor = text_bytes = derived_bytes = block_count = 0

    def normalize(value: object) -> str:
        text = _string(value)
        if len(text.encode("utf-8")) > limits.block_bytes:
            raise ValueError("单块正文超出字节预算")
        return normalizer.normalize(ExtractedDocument(text)).markdown

    def append(
        text: str,
        pointer: str,
        parent: str,
        block_type: str,
        role: str,
        bbox: BBox | None,
        continues: bool,
        *,
        page_number: int,
        fragments: list[str],
    ) -> None:
        nonlocal cursor, text_bytes
        if not text:
            return
        if len(evidence) >= limits.evidence_units:
            raise ValueError("证据片段数量超出预算")
        separator = 2 if cursor else 0
        text_bytes += separator + len(text.encode("utf-8"))
        if text_bytes > limits.text_bytes:
            raise ValueError("新 canonical 超出字节预算")
        cursor += separator
        evidence.append(
            EvidenceSpan(
                pointer,
                parent,
                page_number,
                block_type,
                role,
                "auxiliary" if block_type in _AUXILIARY else "extraction",
                cursor,
                cursor + len(text),
                () if block_type in _AUXILIARY else tuple(t for _, t in headings),
                bbox,
                continues,
            )
        )
        cursor += len(text)
        fragments.append(text)

    for page_idx, value in enumerate(pages):
        page = _object(value)
        if type(page.get("page_idx")) is not int or page["page_idx"] != page_idx:
            raise ValueError("物理页必须从0严格连续，不能猜页")
        blocks = _array(page.get("blocks"))
        fragments: list[str] = []

        for block_idx, item in enumerate(blocks):
            block_count += 1
            if block_count > limits.blocks:
                raise ValueError("原生块数超出预算")
            block = _object(item)
            kind = _string(block.get("type"))
            if (
                kind not in _TEXT | _AUXILIARY | _TITLES | _VISUAL
                or set(block) - _FIELDS
            ):
                raise ValueError("遇到尚未核验的块类型或字段")
            bbox = _bbox(block.get("bbox"))
            if "level" in block and kind not in _TITLES:
                raise ValueError("非标题块不能携带标题层级")
            continues = block.get("continues_prev", False)
            if type(continues) is not bool:
                raise ValueError("continues_prev 必须是 bool")
            pointer = f"/pages/{page_idx}/blocks/{block_idx}"
            text = normalize(block.get("content"))
            if kind in _TITLES:
                level = block.get("level")
                valid_levels = {1} if kind == "doc_title" else {2, 3, 4, 5, 6}
                if type(level) is not int or level not in valid_levels or not text:
                    raise ValueError("标题层级或正文无效")
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, text))
            if kind in {"image", "chart"}:
                derived_bytes += len(text.encode("utf-8"))
                if derived_bytes > limits.interpretation_bytes:
                    raise ValueError("图像解读超出独立字节预算")
                if text:
                    interpretations.append(
                        Interpretation(
                            pointer + "/content", page_idx + 1, kind, text, bbox
                        )
                    )
            else:
                append(
                    text,
                    pointer + "/content",
                    pointer,
                    kind,
                    "body",
                    bbox,
                    continues,
                    page_number=page_idx + 1,
                    fragments=fragments,
                )
            if kind in _VISUAL:
                if "image_source" in block:
                    locator = _string(block["image_source"]).encode("utf-8")
                    assets.append(
                        AssetFact(
                            pointer,
                            page_idx + 1,
                            sha256(locator).hexdigest(),
                            len(locator),
                        )
                    )
                for group in ("captions", "footnotes"):
                    annotations = _array(block.get(group))
                    if len(annotations) > limits.annotations_per_group:
                        raise ValueError("视觉说明数量超出预算")
                    for index, annotation in enumerate(annotations):
                        note = _object(annotation)
                        if set(note) - {"content", "bbox", "anchor"}:
                            raise ValueError("视觉说明包含未核验字段")
                        note_bbox = _bbox(note["bbox"]) if "bbox" in note else None
                        append(
                            normalize(note.get("content")),
                            f"{pointer}/{group}/{index}/content",
                            pointer,
                            kind,
                            group,
                            note_bbox,
                            False,
                            page_number=page_idx + 1,
                            fragments=fragments,
                        )
            elif set(block) & {"captions", "footnotes", "image_source"}:
                raise ValueError("非视觉块不能携带视觉说明或资产")
        extracted_pages.append(ExtractedPage(page_idx + 1, "\n\n".join(fragments)))

    markdown = "\n\n".join(p.markdown for p in extracted_pages if p.markdown)
    document = ExtractedDocument(markdown, pages=tuple(extracted_pages))
    normalized = normalizer.normalize(document)
    if normalized.markdown != markdown or len(markdown) != cursor:
        raise ValueError("跨片段规范化改变正文，区间契约不成立")
    if [b.page_number for b in normalized.blocks] != [
        p.page_number for p in extracted_pages if p.markdown
    ]:
        raise ValueError("规范化未保持页面硬边界")
    identity = json.dumps(
        {
            "policy": POLICY,
            "source_sha256": source_sha256,
            "pages": [(p.page_number, p.markdown) for p in extracted_pages],
            "evidence": [asdict(s) for s in evidence],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return StructuredProjection(
        source_sha256,
        sha256(raw).hexdigest(),
        sha256(identity.encode()).hexdigest(),
        sha256(markdown.encode()).hexdigest(),
        document,
        tuple(evidence),
        tuple(interpretations),
        tuple(assets),
    )
