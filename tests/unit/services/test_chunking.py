"""M4-T4 共享 Chunking Service 与物化边界。"""

from __future__ import annotations

import json

import pytest

from comet_rag.engines.chunkers import ChunkDraft
from comet_rag.engines.pipelines import HookRegistry, HooksState, PipelineConfig
from comet_rag.engines.utils import compute_sha256
from comet_rag.ports import DocumentResourceLimitExceeded, NormalizedDocument
from comet_rag.services.chunking import (
    ChunkingService,
    chunk_id,
    dump_chunk_drafts,
    load_chunk_drafts,
    materialize_chunk_drafts,
)


def test_service_passes_the_complete_document_to_the_strategy() -> None:
    hooks = HookRegistry(HooksState({}, {}))
    received: list[NormalizedDocument] = []

    @hooks.document_chunker("MD")
    def split(
        document: NormalizedDocument, config: PipelineConfig
    ) -> list[ChunkDraft]:
        received.append(document)
        return [
            ChunkDraft(
                text=document.markdown,
                ordinal=0,
                start_char=0,
                end_char=len(document.markdown),
                metadata={"heading_path": ["标题"]},
            )
        ]

    document = NormalizedDocument(
        markdown="标题\n\n正文", metadata={"provider": "fixture"}
    )
    drafts = ChunkingService(PipelineConfig(), hooks).split("md", document)

    assert received == [document]
    assert drafts[0].metadata == {"heading_path": ["标题"]}


@pytest.mark.parametrize(
    "drafts",
    [
        (ChunkDraft(text="x", ordinal=0),),
        [ChunkDraft(text="x", ordinal=1)],
        [ChunkDraft(text="错", ordinal=0, start_char=0, end_char=1)],
        [ChunkDraft(text="x", ordinal=0, start_char=0, end_char=2)],
    ],
)
def test_service_rejects_invalid_strategy_results(drafts: object) -> None:
    hooks = HookRegistry(HooksState({}, {}))

    @hooks.document_chunker("stub")
    def split(
        document: NormalizedDocument, config: PipelineConfig
    ) -> list[ChunkDraft]:
        return drafts  # type: ignore[return-value]

    with pytest.raises((TypeError, ValueError)):
        ChunkingService(PipelineConfig(), hooks).split(
            "stub", NormalizedDocument(markdown="x")
        )


def test_task_payload_round_trips_without_mapping_proxy_or_dataclass() -> None:
    drafts = [
        ChunkDraft(
            text="正文",
            ordinal=0,
            start_char=2,
            end_char=4,
            metadata={"page_number": 3},
        )
    ]

    payload = dump_chunk_drafts(drafts)

    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
    assert load_chunk_drafts(payload) == drafts


def test_structural_metadata_is_stable_across_json_task_handoff() -> None:
    drafts = [
        ChunkDraft(
            text="正文",
            ordinal=0,
            start_char=0,
            end_char=2,
            metadata={
                "block_kind": "section",
                "heading_path": ["安装", "Docker"],
            },
        )
    ]

    payload = dump_chunk_drafts(drafts)
    json_payload = json.loads(json.dumps(payload, ensure_ascii=False))

    assert json_payload == payload
    assert load_chunk_drafts(json_payload) == drafts


def test_task_payload_rejects_non_json_metadata_and_excess_size() -> None:
    non_json = [ChunkDraft(text="x", ordinal=0, metadata={"bad": object()})]
    with pytest.raises(TypeError, match="JSON"):
        dump_chunk_drafts(non_json)

    with pytest.raises(DocumentResourceLimitExceeded, match="payload"):
        dump_chunk_drafts([ChunkDraft(text="x" * 100, ordinal=0)], max_bytes=20)


def test_task_payload_rejects_bool_as_an_integer() -> None:
    payload = [
        {
            "text": "x",
            "ordinal": True,
            "start_char": None,
            "end_char": None,
            "metadata": {},
        }
    ]

    with pytest.raises(TypeError, match="ordinal"):
        load_chunk_drafts(payload)


def test_task_payload_rejects_unknown_fields() -> None:
    payload = [
        {
            "text": "x",
            "ordinal": 0,
            "start_char": None,
            "end_char": None,
            "metadata": {},
            "embedding": [0.1],
        }
    ]

    with pytest.raises(ValueError, match="未知字段"):
        load_chunk_drafts(payload)


@pytest.mark.parametrize("ordinal", [True, -1])
def test_chunk_id_rejects_invalid_ordinal(ordinal: int) -> None:
    with pytest.raises((TypeError, ValueError), match="ordinal"):
        chunk_id("source-id", ordinal)


def test_materialization_owns_id_positions_and_metadata_precedence() -> None:
    drafts = [
        ChunkDraft(
            text="正文",
            ordinal=0,
            start_char=4,
            end_char=6,
            metadata={"heading_path": ["真实标题"], "scope": "chunk"},
        )
    ]

    chunks = materialize_chunk_drafts(
        drafts,
        source_id="source-id",
        source="s3://bucket/file.md",
        file_type="md",
        document_metadata={"scope": "document", "source_id": "forged"},
        request_metadata={
            "scope": "request",
            "heading_path": ["伪造标题"],
            "kb_id": "forged",
        },
        kb_id="kb-real",
    )

    assert chunks[0].id == chunk_id("source-id", 0) == compute_sha256("source-id:0")
    assert chunks[0].metadata == {
        "scope": "chunk",
        "heading_path": ["真实标题"],
        "source": "s3://bucket/file.md",
        "source_id": "source-id",
        "file_type": "md",
        "total_chunks": 1,
        "chunk_index": 0,
        "kb_id": "kb-real",
        "chunk_start": 4,
        "chunk_end": 6,
    }


def test_materialization_rejects_forged_chunk_facts_and_system_fields() -> None:
    document_metadata = {
        "label": "document",
        "provider": "mineru",
        "source_id": "forged-document",
    }
    request_metadata = {
        "label": "request",
        "department": "研发",
        "heading_path": ["伪造标题"],
        "parent_id": "forged-request",
        "page_number": 999,
    }
    draft_metadata = {
        "label": "chunk",
        "page_number": 7,
        "source": "forged-chunk",
    }

    chunks = materialize_chunk_drafts(
        [ChunkDraft(text="正文", ordinal=0, metadata=draft_metadata)],
        source_id="trusted-source",
        source="s3://bucket/report.pdf",
        file_type="pdf",
        document_metadata=document_metadata,
        request_metadata=request_metadata,
    )

    assert chunks[0].metadata == {
        "label": "chunk",
        "provider": "mineru",
        "department": "研发",
        "page_number": 7,
        "source": "s3://bucket/report.pdf",
        "source_id": "trusted-source",
        "file_type": "pdf",
        "total_chunks": 1,
        "chunk_index": 0,
    }
    assert "parent_id" not in chunks[0].metadata
    assert "heading_path" not in chunks[0].metadata
    assert document_metadata["source_id"] == "forged-document"
    assert request_metadata["page_number"] == 999
    assert draft_metadata["source"] == "forged-chunk"
