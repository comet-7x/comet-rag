from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.documents.normalization import MarkdownDocumentNormalizer
from comet_rag.infrastructure.evidence import bundle as bundles
from comet_rag.infrastructure.evidence.bundle import (
    EvidenceReference,
    build_bundle,
    load_bundle,
    publish_bundle,
)
from comet_rag.infrastructure.persistence.document_store.memory import (
    InMemoryDocumentStore,
)
from comet_rag.ports.document_store import RevisionDraft, RevisionKey, StoredParent
from evals.pdf.page_sections import page_sections, split_page_sections
from tests.unit.test_pdf_structured import SOURCE, block, fixture

KEY = RevisionKey("evidence-kb", "source", "revision")


def packet():
    return build_bundle(
        json.dumps(fixture()).encode(), key=KEY, source_sha256=SOURCE, pages=3
    )


def test_bundle_round_trip_pins_identity_and_keeps_derived_text_separate(
    tmp_path: Path,
) -> None:
    bundle = packet()
    out = tmp_path / "bundle"
    publish_bundle(bundle, out)
    restored = load_bundle(
        out, EvidenceReference.from_payload(bundle.reference.payload())
    )
    assert restored.canonical_sha256 == bundle.reference.canonical_sha256
    assert [p.page_number for p in restored.document.pages] == [1, 2, 3]
    assert restored.document.pages[1].markdown == ""
    assert [i.text for i in restored.interpretations] == ["Estimated 99"]
    assert "Estimated 99" not in restored.document.markdown
    assert len(bundle.reference.payload()) == 6
    assert len(json.dumps(bundle.reference.payload()).encode()) < 1024
    assert bundle.reference.prefix.startswith("evidence/v1/")
    assert "evidence-kb" not in bundle.reference.prefix
    with pytest.raises(FileExistsError):
        publish_bundle(bundle, out)
    assert load_bundle(out, bundle.reference).input_sha256 == restored.input_sha256


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_failed_publication_has_no_usable_commit_marker(
    tmp_path: Path, monkeypatch, failure_at: int
) -> None:
    bundle = packet()
    original = bundles._write
    count = 0

    def fail(path, data):
        nonlocal count
        count += 1
        if count == failure_at:
            raise OSError("injected write failure")
        original(path, data)

    monkeypatch.setattr(bundles, "_write", fail)
    out = tmp_path / "failed"
    with pytest.raises(OSError, match="injected"):
        publish_bundle(bundle, out)
    assert not (out / "manifest.json").exists()
    with pytest.raises(ValueError):
        load_bundle(out, bundle.reference)


@pytest.mark.parametrize(
    "fault", ["raw", "ledger", "manifest", "extra_file", "cross_revision"]
)
def test_mutated_bundle_cannot_be_read_as_original(tmp_path: Path, fault: str) -> None:
    bundle = packet()
    out = tmp_path / "bundle"
    publish_bundle(bundle, out)
    expected = bundle.reference
    if fault == "extra_file":
        (out / "extra.json").write_text("{}")
    elif fault == "cross_revision":
        expected = replace(expected, key=RevisionKey("other", "source", "revision"))
    else:
        name = {
            "raw": "structured_content.json",
            "ledger": "ledger.json",
            "manifest": "manifest.json",
        }[fault]
        with (out / name).open("ab") as stream:
            stream.write(b" ")  # 仍是合法JSON，不能靠解析异常冒充摘要守卫生效。
    with pytest.raises(ValueError):
        load_bundle(out, expected)


@pytest.mark.parametrize(
    "fault", ["heading", "page_bool", "derived", "schema_bool", "canonical"]
)
def test_rehashed_ledger_still_has_to_match_native_semantics(
    tmp_path: Path, fault: str
) -> None:
    bundle = packet()
    out = tmp_path / "bundle"
    publish_bundle(bundle, out)
    manifest = json.loads(bundle.manifest)
    ledger = json.loads(bundle.ledger)
    if fault == "schema_bool":
        manifest["schema_version"] = True
    elif fault == "canonical":
        manifest["canonical_sha256"] = "f" * 64
    else:
        if fault == "heading":
            ledger["evidence"][2]["heading_path"] = ["Forged heading"]
        elif fault == "page_bool":
            ledger["pages"][0]["page_number"] = True
        else:
            ledger["markdown"] += "Estimated 99"
        data = json.dumps(ledger).encode()
        (out / "ledger.json").write_bytes(data)
        manifest["files"]["ledger.json"] = {
            "bytes": len(data),
            "sha256": sha256(data).hexdigest(),
        }
    raw = json.dumps(manifest).encode()
    (out / "manifest.json").write_bytes(raw)
    expected = replace(bundle.reference, manifest_sha256=sha256(raw).hexdigest())
    if fault == "canonical":
        expected = replace(expected, canonical_sha256="f" * 64)
    with pytest.raises(ValueError):
        load_bundle(out, expected)


def test_semantic_ledger_budget_is_enforced_before_publication(monkeypatch) -> None:
    monkeypatch.setattr(bundles, "LEDGER_BYTES", 10)
    with pytest.raises(ValueError, match="账本超出"):
        packet()


@pytest.mark.parametrize("field", ["schema_version", "pages"])
def test_reference_rejects_bool_in_integer_identity(field: str) -> None:
    payload = packet().reference.payload()
    payload[field] = True
    with pytest.raises(ValueError):
        EvidenceReference.from_payload(payload)


def test_page_heading_view_keeps_offsets_and_duplicate_page_sections(
    tmp_path: Path,
) -> None:
    bundle = packet()
    publish_bundle(bundle, tmp_path / "bundle")
    result = load_bundle(tmp_path / "bundle", bundle.reference)
    view = page_sections(result)
    assert view.markdown == result.document.markdown
    assert [b.page_number for b in view.blocks] == [1, 1, 3, 3, 3, 3]
    assert [b.heading_path for b in view.blocks] == [
        ("Report",),
        ("Report", "Scope"),
        ("Report", "Scope"),
        ("Report", "Scope", "Deep"),
        ("Report", "Scope", "Next"),
        (),
    ]
    drafts = split_page_sections(
        result, FixedSizeChunker(chunk_size=20, chunk_overlap=3)
    )
    assert drafts
    for draft in drafts:
        start, end = draft.start_char, draft.end_char
        assert start is not None and end is not None
        assert result.document.markdown[start:end] == draft.text
        assert any(b.start_char <= start < end <= b.end_char for b in view.blocks)
        assert draft.metadata["page_number"] in {1, 3}
    # 生产normalizer仍是原page语义，没有借本轮原型暗改默认行为。
    assert all(
        not b.heading_path
        for b in MarkdownDocumentNormalizer().normalize(result.document).blocks
    )


def test_same_title_occurrences_and_auxiliary_blocks_remain_boundaries(
    tmp_path: Path,
) -> None:
    data = fixture()
    data["pages"][0]["blocks"] = [
        block("paragraph_title", "Repeated", level=2),
        block("text", "First"),
        block("paragraph_title", "Repeated", level=2),
        block("text", "Second"),
        block("footer", "Auxiliary"),
        block("text", "Third"),
    ]
    bundle = build_bundle(
        json.dumps(data).encode(), key=KEY, source_sha256=SOURCE, pages=3
    )
    publish_bundle(bundle, tmp_path / "bundle")
    result = load_bundle(tmp_path / "bundle", bundle.reference)
    blocks = [b for b in page_sections(result).blocks if b.page_number == 1]
    assert [result.document.markdown[b.start_char : b.end_char] for b in blocks] == [
        "Repeated\n\nFirst",
        "Repeated\n\nSecond",
        "Auxiliary",
        "Third",
    ]


def test_dropped_evidence_is_not_silently_indexed_with_wrong_heading(
    tmp_path: Path,
) -> None:
    bundle = packet()
    publish_bundle(bundle, tmp_path / "bundle")
    result = load_bundle(tmp_path / "bundle", bundle.reference)
    bad = replace(result, evidence=result.evidence[:2])
    with pytest.raises(ValueError, match="遗漏"):
        split_page_sections(bad, FixedSizeChunker(1000))


def test_internal_evidence_gap_cannot_be_hidden_by_merging_same_heading(
    tmp_path: Path,
) -> None:
    bundle = packet()
    publish_bundle(bundle, tmp_path / "bundle")
    result = load_bundle(tmp_path / "bundle", bundle.reference)
    assert result.evidence[2].block_type == "text"
    bad = replace(result, evidence=result.evidence[:2] + result.evidence[3:])
    with pytest.raises(ValueError, match="遗漏"):
        split_page_sections(bad, FixedSizeChunker(1000))


async def test_existing_document_store_keeps_only_compact_parent_reference(
    tmp_path: Path,
) -> None:
    bundle = packet()
    publish_bundle(bundle, tmp_path / "bundle")
    result = load_bundle(tmp_path / "bundle", bundle.reference)
    store = InMemoryDocumentStore()
    draft = RevisionDraft(KEY, "b" * 64, 1, 0, "test.pdf", "pdf")
    await store.reserve_revision(draft)
    parent = StoredParent(
        KEY,
        "parent",
        0,
        result.document.markdown,
        0,
        len(result.document.markdown),
        metadata={"evidence_reference": bundle.reference.payload()},
    )
    await store.put_parents(KEY, [parent])
    await store.mark_ready(KEY)
    await store.activate_revision(KEY)
    recovered = (await store.get_parents(KEY.kb_id, ["parent"]))["parent"]
    reference = EvidenceReference.from_payload(recovered.metadata["evidence_reference"])
    assert (
        load_bundle(tmp_path / "bundle", reference).document.markdown == recovered.text
    )
    saved_reference = recovered.metadata["evidence_reference"]
    assert isinstance(saved_reference, dict)
    saved_reference["pages"] = 99
    reread = (await store.get_parents(KEY.kb_id, ["parent"]))["parent"].metadata[
        "evidence_reference"
    ]
    assert isinstance(reread, dict)
    assert reread["pages"] == 3
