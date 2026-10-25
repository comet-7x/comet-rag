from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import httpx

from comet_rag.engines.chunkers import RecursiveChunker
from comet_rag.engines.chunkers.strategies import PageChunkingStrategy
from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer
from comet_rag.infrastructure.extractors import AsyncMinerUDocumentExtractor
from comet_rag.ports import AsyncDocumentExtractorPort, ExtractedDocument
from evals.pdf.data import DATA, PDFSample, PDFSnapshot
from evals.retrieval.data import read_json, write_json


def page_summary(document: ExtractedDocument, total_pages: int) -> dict[str, object]:
    """页事实与正文必须同时成立；无事实是明确的 section 回退，不算页验收成功。"""
    normalized = MarkdownDocumentNormalizer().normalize(document)
    if not normalized.markdown:
        raise ValueError("真实 PDF 解析返回空正文")
    numbers = [page.page_number for page in document.pages]
    if numbers:
        if (
            numbers != sorted(set(numbers))
            or not 1 <= numbers[0] <= numbers[-1] <= total_pages
        ):
            raise ValueError("解析页号必须严格递增且位于原 PDF 内")
        if "\n\n".join(p.markdown for p in document.pages) != document.markdown:
            raise ValueError("页片段未精确重建原生 Markdown")
        if [b.page_number for b in normalized.blocks] != numbers:
            raise ValueError("规范化丢失了原生页事实")
        drafts = PageChunkingStrategy(RecursiveChunker(800, 80)).split(normalized)
        if not drafts:
            raise ValueError("真实页分块不能是空结果")
        for draft in drafts:
            start, end = draft.start_char, draft.end_char
            if (
                start is None
                or end is None
                or draft.text != normalized.markdown[start:end]
            ):
                raise ValueError("分块不再对应规范化正文区间")
            if not any(
                b.page_number == draft.metadata.get("page_number")
                and b.start_char <= start < end <= b.end_char
                for b in normalized.blocks
            ):
                raise ValueError("分块跨越物理页硬边界")
    return {
        "page_status": "verified" if numbers else "unavailable_section_fallback",
        "page_numbers": numbers,
        "normalized_sha256": sha256(normalized.markdown.encode()).hexdigest(),
        "normalized_characters": len(normalized.markdown),
        "block_count": len(normalized.blocks),
    }


def _verify_pdf(path: Path, sample: PDFSample) -> str:
    if path.stat().st_size != sample.pdf_bytes:
        raise ValueError("原 PDF 字节与锁定来源不一致")
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != sample.pdf_sha256:
        raise ValueError("原 PDF 字节与锁定来源不一致")
    return digest


async def audit(
    extractor: AsyncDocumentExtractorPort,
    pdf_dir: Path,
    out: Path,
    *,
    data: Path = DATA,
    require_pages: bool = False,
) -> None:
    """只上传来源锁里的公开 PDF；不复用 pypdf 字符 span 给原生解析结果评分。"""
    snapshot = read_json(data / "pages.json", PDFSnapshot)
    sources: list[tuple[Path, PDFSample]] = []
    # 所有文件先核验，再进行第一次外部调用；不在中途才发现上传了错误资料。
    for sample in snapshot.samples:
        if not sample.source.id.isalnum():
            raise ValueError("PDF 来源 id 不可用于文件名")
        path = pdf_dir / f"{sample.source.id}.pdf"
        _verify_pdf(path, sample)
        sources.append((path, sample))
    # 新目录避免失败运行旁边残留上次的成功报告，被误当成本轮结果。
    out.mkdir(parents=True, exist_ok=False)
    samples = []
    for path, sample in sources:
        started = perf_counter()
        document = await extractor.extract(
            path, filename=path.name, media_type="application/pdf"
        )
        summary = page_summary(document, sample.total_pages)
        if require_pages and summary["page_status"] != "verified":
            raise ValueError("本次验收要求原生页事实，不能以 section 回退通过")
        samples.append(
            {
                "source_id": path.stem,
                "source_sha256": _verify_pdf(path, sample),
                "elapsed_seconds": perf_counter() - started,
                "provider": dict(document.metadata),
                **summary,
            }
        )
        write_json(
            out / f"{path.stem}.json",
            {
                "markdown": document.markdown,
                "pages": [
                    {"page_number": p.page_number, "markdown": p.markdown}
                    for p in document.pages
                ],
            },
        )
    write_json(
        out / "report.json",
        {
            "scope": "native_parse_only; not retrieval_quality_or_fixed_weights_acceptance",
            "snapshot_sha256": sha256((data / "pages.json").read_bytes()).hexdigest(),
            "samples": samples,
        },
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="独立验证公开 PDF 的原生 MinerU 解析与页边界"
    )
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mineru-url", required=True)
    parser.add_argument("--backend", default="vlm-http-client")
    parser.add_argument("--require-pages", action="store_true")
    args = parser.parse_args()
    async with httpx.AsyncClient() as client:
        extractor = AsyncMinerUDocumentExtractor(
            args.mineru_url, backend=args.backend, language="en", client=client
        )
        await audit(extractor, args.pdf_dir, args.out, require_pages=args.require_pages)


if __name__ == "__main__":
    asyncio.run(main())
