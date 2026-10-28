from __future__ import annotations

import json
from dataclasses import replace

import pytest

from comet_rag.engines.chunkers import FixedSizeChunker
from comet_rag.engines.documents.mineru.structured import project_structured
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.evidence.bundle import build_bundle
from comet_rag.ports import NormalizedDocument
from comet_rag.ports.document_store import RevisionConflict, RevisionKey
from comet_rag.services.evidence import (
    ParentEvidenceReference,
    VerifiedEvidence,
    read_evidence,
)
from comet_rag.services.indexing import IndexPlanner, _digest, _snapshot_plan

KEY = RevisionKey("evidence-kb", "source", "revision")


def evidence_fixture():
    raw = json.dumps(
        {
            "metadata": {
                "file_suffix": "pdf",
                "producer": {"name": "mineru", "version": "4.0.12"},
                "document": {"page_count": 1, "page_count_kind": "physical"},
            },
            "is_full_document": True,
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "text",
                            "bbox": [0.1, 0.1, 0.9, 0.9],
                            "content": "Alpha beta gamma. Delta epsilon zeta.",
                        }
                    ],
                }
            ],
        }
    ).encode()
    bundle = build_bundle(raw, key=KEY, source_sha256="a" * 64, pages=1)
    projection = project_structured(raw, source_sha256="a" * 64, expected_pages=1)
    verified = VerifiedEvidence(
        ParentEvidenceReference("documents_v1", bundle.reference), projection
    )
    hierarchy = HierarchyBuilder(FixedSizeChunker(20), FixedSizeChunker(8)).split(
        NormalizedDocument(projection.document.markdown)
    )
    return verified, hierarchy


def make_plan(verified=None, hierarchy=None, **kwargs):
    default, chunks = evidence_fixture()
    return IndexPlanner().plan(
        hierarchy or chunks,
        key=KEY,
        source="source.pdf",
        file_type="pdf",
        evidence=verified or default,
        **kwargs,
    )


def test_verified_parent_reference_is_bound_to_plan_without_vector_metadata() -> None:
    verified, _ = evidence_fixture()
    plan = make_plan()
    assert len(plan.parents) > 1
    assert all(
        p.metadata["evidence_reference"] == verified.reference.payload()
        for p in plan.parents
    )
    assert "evidence_reference" not in plan.metadata
    assert all("evidence_reference" not in c.metadata for c in plan.vector_children)
    _snapshot_plan(plan)
    changed = make_plan(
        verified=replace(
            verified, reference=replace(verified.reference, storage_id="other_store")
        )
    )
    assert changed.plan_sha256 != plan.plan_sha256
    assert [c.id for c in changed.vector_children] == [
        c.id for c in plan.vector_children
    ]


@pytest.mark.parametrize("layer", ["document", "request", "chunk"])
def test_untrusted_metadata_cannot_forge_evidence_even_without_binding(
    layer: str,
) -> None:
    _, hierarchy = evidence_fixture()
    kwargs = {}
    forged = {"evidence_reference": {"storage_id": "attacker"}, "ordinary": "retained"}
    if layer == "chunk":
        hierarchy = replace(
            hierarchy,
            parents=tuple(replace(p, metadata=forged) for p in hierarchy.parents),
            children=tuple(replace(c, metadata=forged) for c in hierarchy.children),
        )
    else:
        kwargs[layer + "_metadata"] = forged
    plan = IndexPlanner().plan(
        hierarchy, key=KEY, source="source.pdf", file_type="pdf", **kwargs
    )
    assert all("evidence_reference" not in p.metadata for p in plan.parents)
    assert all("evidence_reference" not in c.metadata for c in plan.vector_children)
    assert "evidence_reference" not in plan.metadata
    assert all(p.metadata["ordinary"] == "retained" for p in plan.parents)


@pytest.mark.parametrize("fault", ["key", "parent_text", "flat"])
def test_binding_rejects_wrong_revision_or_unproven_parent_text(fault: str) -> None:
    verified, hierarchy = evidence_fixture()
    key = KEY
    if fault == "key":
        key = replace(KEY, revision_id="other")
    elif fault == "parent_text":
        hierarchy = replace(
            hierarchy,
            parents=tuple(
                replace(p, text=p.text.swapcase()) for p in hierarchy.parents
            ),
            children=tuple(
                replace(c, text=c.text.swapcase()) for c in hierarchy.children
            ),
        )
    else:
        hierarchy = FixedSizeChunker(8).split(verified.projection.document.markdown)
    with pytest.raises(ValueError):
        IndexPlanner().plan(
            hierarchy, key=key, source="source.pdf", file_type="pdf", evidence=verified
        )


@pytest.mark.parametrize("fault", ["source", "canonical", "body", "pages"])
def test_read_projection_must_match_trusted_reference(fault: str) -> None:
    verified, _ = evidence_fixture()
    result = verified.projection
    if fault == "source":
        result = replace(result, source_sha256="b" * 64)
    elif fault == "canonical":
        result = replace(result, canonical_sha256="b" * 64)
    elif fault == "body":
        result = replace(result, body_sha256="b" * 64)
    else:
        result = replace(result, document=replace(result.document, pages=()))
    with pytest.raises(ValueError):
        VerifiedEvidence(verified.reference, result)


@pytest.mark.parametrize(
    "fault", ["child", "document", "cross_revision", "partial", "mixed", "damaged"]
)
def test_persisted_plan_rejects_evidence_misplacement_or_identity_corruption(
    fault: str,
) -> None:
    plan = make_plan()
    # 故障载体需要可变的JSON树；不从实现推导预期值。
    payload = json.loads(json.dumps(plan.parents[0].metadata["evidence_reference"]))
    if fault == "child":
        plan = replace(
            plan,
            vector_children=(
                replace(
                    plan.vector_children[0],
                    metadata={
                        **plan.vector_children[0].metadata,
                        "evidence_reference": payload,
                    },
                ),
                *plan.vector_children[1:],
            ),
        )
    elif fault == "document":
        plan = replace(plan, metadata={"evidence_reference": payload})
    else:
        metadata = dict(plan.parents[0].metadata)
        if fault == "partial":
            del metadata["evidence_reference"]
        elif fault == "damaged":
            metadata["evidence_reference"] = None
        elif fault == "mixed":
            metadata["evidence_reference"] = {**payload, "storage_id": "different"}
        else:
            bundle = dict(payload["bundle"])
            bundle["key"] = {**bundle["key"], "revision_id": "other"}
            metadata["evidence_reference"] = {**payload, "bundle": bundle}
        parents = (
            tuple(replace(p, metadata=metadata) for p in plan.parents)
            if fault == "cross_revision"
            else (replace(plan.parents[0], metadata=metadata), *plan.parents[1:])
        )
        plan = replace(plan, parents=parents)
    # 构造指纹自洽的错误载荷，确保拒绝来自引用守卫，而非通用指纹不匹配。
    plan = replace(
        plan,
        plan_sha256=_digest(
            plan.key,
            plan.source,
            plan.file_type,
            plan.metadata,
            plan.parents,
            plan.vector_children,
        ),
    )
    with pytest.raises(RevisionConflict, match="证据"):
        _snapshot_plan(plan)


async def test_reader_uses_exact_storage_identity_and_preserves_store_failure() -> None:
    verified, _ = evidence_fixture()
    calls = []

    class Store:
        async def read(self, reference):
            calls.append(reference)
            return verified.projection

        async def publish(self, bundle):
            raise AssertionError("read path must not write")

    assert (
        await read_evidence({"documents_v1": Store()}, verified.reference) == verified
    )
    assert calls == [verified.reference.bundle]
    with pytest.raises(ValueError, match="未配置"):
        await read_evidence({"default": Store()}, verified.reference)
    assert len(calls) == 1

    class Failed(Store):
        async def read(self, reference):
            raise OSError("injected storage failure")

    with pytest.raises(OSError, match="injected"):
        await read_evidence({"documents_v1": Failed()}, verified.reference)


@pytest.mark.parametrize(
    "storage_id", ["https://example.invalid", "", "Upper", "a" * 65]
)
def test_storage_identity_cannot_be_a_location_or_unbounded_string(
    storage_id: str,
) -> None:
    verified, _ = evidence_fixture()
    with pytest.raises(ValueError):
        replace(verified.reference, storage_id=storage_id)
