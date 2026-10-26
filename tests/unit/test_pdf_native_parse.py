from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from comet_rag.ports import DocumentUpstreamError, ExtractedDocument, ExtractedPage
from evals.pdf.data import DATA
from evals.pdf.parse import audit, page_summary


def test_native_repeated_text_keeps_distinct_page_spans() -> None:
    result = page_summary(
        ExtractedDocument(
            "Same\n\nSame", pages=(ExtractedPage(1, "Same"), ExtractedPage(2, "Same"))
        ),
        2,
    )
    assert result["page_status"] == "verified"
    assert result["page_numbers"] == [1, 2]
    assert result["block_count"] == 2


def test_missing_pages_are_not_reported_as_page_acceptance() -> None:
    result = page_summary(
        ExtractedDocument("# A\n\n<table><tr><td>B</td></tr></table>"), 7
    )
    assert result["page_status"] == "unavailable_section_fallback"
    assert result["page_numbers"] == []


@pytest.mark.parametrize("fault", ["overflow", "mismatch", "raw_mismatch", "empty"])
def test_native_page_audit_rejects_false_facts(fault: str) -> None:
    document = ExtractedDocument("Same", pages=(ExtractedPage(3, "Same"),))
    if fault == "mismatch":
        document = ExtractedDocument("Other", pages=(ExtractedPage(1, "Same"),))
    elif fault == "raw_mismatch":
        document = ExtractedDocument("Same  ", pages=(ExtractedPage(1, "Same"),))
    elif fault == "empty":
        document = ExtractedDocument(" \n")
    with pytest.raises(ValueError):
        page_summary(document, 2)


class RecordingExtractor:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[Path] = []
        self.fail = fail

    async def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls.append(path)
        assert filename == path.name and media_type == "application/pdf"
        if self.fail:
            raise DocumentUpstreamError("Injected parser failure")
        return ExtractedDocument("Native output", metadata={"provider": "fixture"})

    async def close(self) -> None:
        pass


def locked_sources(tmp_path: Path) -> Path:
    snapshot = json.loads((DATA / "pages.json").read_text())
    for sample in snapshot["samples"]:
        raw = b"%PDF-1.4\nTest bytes - never sent to a real parser"
        (tmp_path / f"{sample['source']['id']}.pdf").write_bytes(raw)
        sample["pdf_bytes"] = len(raw)
        sample["pdf_sha256"] = sha256(raw).hexdigest()
    (tmp_path / "pages.json").write_text(json.dumps(snapshot))
    return tmp_path


async def test_audit_records_fallback_without_retrieval_or_weight_claim(
    tmp_path: Path,
) -> None:
    data = locked_sources(tmp_path)
    out = tmp_path / "output"
    extractor = RecordingExtractor()
    await audit(extractor, tmp_path, out, data=data)
    report = json.loads((out / "report.json").read_text())
    assert len(extractor.calls) == 2
    assert "not retrieval_quality_or_fixed_weights_acceptance" in report["scope"]
    assert [s["page_status"] for s in report["samples"]] == [
        "unavailable_section_fallback"
    ] * 2
    assert json.loads((out / "nist145.json").read_text())["markdown"] == "Native output"
    assert "mineru-url" not in json.dumps(report)


@pytest.mark.parametrize("fault", ["size", "digest"])
async def test_all_original_bytes_checked_before_any_upload(
    tmp_path: Path, fault: str
) -> None:
    data = locked_sources(tmp_path)
    source = tmp_path / "nist144.pdf"
    source.write_bytes(
        b"Wrong file" if fault == "size" else b"X" * source.stat().st_size
    )
    extractor = RecordingExtractor()
    with pytest.raises(ValueError, match="字节"):
        await audit(extractor, tmp_path, tmp_path / "out", data=data)
    assert extractor.calls == []
    assert not (tmp_path / "out").exists()


async def test_parser_failure_never_writes_success_report(tmp_path: Path) -> None:
    data = locked_sources(tmp_path)
    extractor = RecordingExtractor(fail=True)
    with pytest.raises(DocumentUpstreamError, match="Injected"):
        await audit(extractor, tmp_path, tmp_path / "out", data=data)
    assert len(extractor.calls) == 1
    assert not (tmp_path / "out/report.json").exists()


async def test_existing_success_report_cannot_mask_a_failed_rerun(
    tmp_path: Path,
) -> None:
    data = locked_sources(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    report = out / "report.json"
    report.write_text("Old run")
    extractor = RecordingExtractor(fail=True)
    with pytest.raises(FileExistsError):
        await audit(extractor, tmp_path, out, data=data)
    assert extractor.calls == []
    assert report.read_text() == "Old run"


@pytest.mark.parametrize("fault", ["text", "cross_page", "empty"])
def test_native_audit_detects_invalid_chunker_results(monkeypatch, fault: str) -> None:
    from comet_rag.engines.chunkers.types import ChunkDraft
    from evals.pdf.parse import PageChunkingStrategy

    drafts = [ChunkDraft("Wrong", 0, 0, 4, {"page_number": 1})]
    if fault == "cross_page":
        drafts = [ChunkDraft("Same\n\nSame", 0, 0, 10, {"page_number": 1})]
    elif fault == "empty":
        drafts = []
    monkeypatch.setattr(PageChunkingStrategy, "split", lambda *_: drafts)
    with pytest.raises(ValueError):
        page_summary(
            ExtractedDocument(
                "Same\n\nSame",
                pages=(ExtractedPage(1, "Same"), ExtractedPage(2, "Same")),
            ),
            2,
        )


async def test_changed_source_during_parsing_is_not_accepted(tmp_path: Path) -> None:
    data = locked_sources(tmp_path)

    class ChangedSource(RecordingExtractor):
        async def extract(
            self, path: Path, /, *, filename: str, media_type: str
        ) -> ExtractedDocument:
            path.write_bytes(b"X" * path.stat().st_size)
            return await super().extract(path, filename=filename, media_type=media_type)

    with pytest.raises(ValueError, match="字节"):
        await audit(ChangedSource(), tmp_path, tmp_path / "out", data=data)
    assert not (tmp_path / "out/report.json").exists()


async def test_required_native_pages_cannot_pass_with_section_fallback(
    tmp_path: Path,
) -> None:
    data = locked_sources(tmp_path)
    with pytest.raises(ValueError, match="原生页事实"):
        await audit(
            RecordingExtractor(),
            tmp_path,
            tmp_path / "out",
            data=data,
            require_pages=True,
        )
    assert not (tmp_path / "out/report.json").exists()


async def test_required_native_pages_reject_partial_but_consistent_output(
    tmp_path: Path,
) -> None:
    data = locked_sources(tmp_path)

    class Partial(RecordingExtractor):
        async def extract(
            self, path: Path, /, *, filename: str, media_type: str
        ) -> ExtractedDocument:
            return ExtractedDocument(
                "first\n\nlast",
                pages=(ExtractedPage(1, "first"), ExtractedPage(7, "last")),
            )

    with pytest.raises(ValueError, match="完整"):
        await audit(
            Partial(), tmp_path, tmp_path / "out", data=data, require_pages=True
        )
    assert not (tmp_path / "out/report.json").exists()


@pytest.mark.parametrize("blank", ["", " "])
def test_native_physical_empty_pages_have_no_fabricated_content_block(
    blank: str,
) -> None:
    fragments = ("First", blank, "Last", "")
    result = page_summary(
        ExtractedDocument(
            "\n\n".join(p for p in fragments if p),
            pages=tuple(ExtractedPage(i + 1, p) for i, p in enumerate(fragments)),
        ),
        4,
    )
    assert result["page_numbers"] == [1, 2, 3, 4]
    assert result["normalized_empty_page_numbers"] == [2, 4]
    assert result["block_count"] == 2


def test_normalizer_losing_nonempty_page_still_fails_audit(monkeypatch) -> None:
    from dataclasses import replace

    from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer

    original = MarkdownDocumentNormalizer.normalize

    def lose(self, document):
        result = original(self, document)
        return replace(result, blocks=result.blocks[:1]) if document.pages else result

    monkeypatch.setattr(MarkdownDocumentNormalizer, "normalize", lose)
    with pytest.raises(ValueError, match="规范化丢失"):
        page_summary(
            ExtractedDocument(
                "First\n\nLast",
                pages=(ExtractedPage(1, "First"), ExtractedPage(2, "Last")),
            ),
            2,
        )
