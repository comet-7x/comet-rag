"""M4 Chunking 契约：值对象、策略边界与旧 Hook 兼容。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from comet_rag.engines.chunkers import (
    ChunkDraft,
    ChunkingStrategy,
    merge_chunk_metadata,
)
from comet_rag.engines.pipelines import (
    HooksState,
    PipelineConfig,
    PipelineHooks,
    adapt_legacy_chunk_hook,
)
from comet_rag.ports import NormalizedDocument


class StubStrategy:
    def split(self, document: NormalizedDocument, /) -> list[ChunkDraft]:
        return [
            ChunkDraft(
                text=document.markdown,
                ordinal=0,
                start_char=0,
                end_char=len(document.markdown),
            )
        ]


def test_strategy_is_structural_and_sync_only() -> None:
    strategy = StubStrategy()

    assert isinstance(strategy, ChunkingStrategy)
    assert not hasattr(strategy, "asplit")


def test_chunk_draft_copies_and_freezes_metadata() -> None:
    source = {"page_number": 3}
    draft = ChunkDraft(text="正文", ordinal=0, metadata=source)
    source["page_number"] = 99

    assert draft.metadata == {"page_number": 3}
    with pytest.raises(TypeError):
        cast("dict[str, object]", draft.metadata)["page_number"] = 4
    with pytest.raises(FrozenInstanceError):
        draft.ordinal = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"text": "  ", "ordinal": 0}, "空白文本"),
        ({"text": "正文", "ordinal": -1}, "ordinal"),
        ({"text": "正文", "ordinal": 0, "start_char": 0}, "同时提供"),
        (
            {"text": "正文", "ordinal": 0, "start_char": -1, "end_char": 1},
            "start_char",
        ),
        (
            {"text": "正文", "ordinal": 0, "start_char": 2, "end_char": 2},
            "end_char",
        ),
        ({"text": "正文", "ordinal": 0, "metadata": {"": 1}}, "非空字符串"),
    ],
)
def test_chunk_draft_rejects_invalid_state(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ChunkDraft(**kwargs)  # type: ignore[arg-type]


def test_metadata_precedence_and_reserved_keys() -> None:
    result = merge_chunk_metadata(
        document={
            "label": "document",
            "provider": "mineru",
            "source_id": "forged-document",
        },
        request={
            "label": "request",
            "department": "研发",
            "heading_path": ["伪造标题"],
            "parent_id": "forged-request",
            "page_number": 999,
        },
        chunk={
            "label": "chunk",
            "page_number": 7,
            "source": "forged-chunk",
        },
        system={
            "source": "s3://bucket/report.pdf",
            "source_id": "trusted-source",
            "chunk_index": 2,
        },
    )

    assert result == {
        "label": "chunk",
        "provider": "mineru",
        "department": "研发",
        "page_number": 7,
        "source": "s3://bucket/report.pdf",
        "source_id": "trusted-source",
        "chunk_index": 2,
    }
    assert "parent_id" not in result
    assert "heading_path" not in result


def test_metadata_merge_does_not_mutate_inputs() -> None:
    document = {"scope": "document"}
    request = {"scope": "request"}
    chunk = {"scope": "chunk"}
    system = {"scope": "system"}

    assert merge_chunk_metadata(
        document=document,
        request=request,
        chunk=chunk,
        system=system,
    ) == {"scope": "system"}
    assert document == {"scope": "document"}
    assert request == {"scope": "request"}
    assert chunk == {"scope": "chunk"}
    assert system == {"scope": "system"}


def test_legacy_adapter_uses_markdown_without_guessing_positions() -> None:
    received: list[str] = []

    def legacy(text: str, config: PipelineConfig) -> list[str]:
        received.append(text)
        return ["重复", "重复"]

    document = NormalizedDocument(
        markdown="重复重复",
        metadata={"provider": "fixture"},
    )
    drafts = adapt_legacy_chunk_hook(legacy)(document, PipelineConfig())

    assert received == ["重复重复"]
    assert [draft.ordinal for draft in drafts] == [0, 1]
    assert [draft.text for draft in drafts] == ["重复", "重复"]
    assert all(draft.start_char is None and draft.end_char is None for draft in drafts)
    assert all(not draft.metadata for draft in drafts)


@pytest.mark.parametrize("bad_result", [("tuple",), [123], [" "]])
def test_legacy_adapter_rejects_invalid_results(bad_result: object) -> None:
    def legacy(text: str, config: PipelineConfig) -> list[str]:
        return cast("list[str]", bad_result)

    with pytest.raises((TypeError, ValueError)):
        adapt_legacy_chunk_hook(legacy)(NormalizedDocument(markdown="正文"), PipelineConfig())


def test_document_chunker_registration_is_case_insensitive() -> None:
    @PipelineHooks.document_chunker("PDF")
    def chunk(
        document: NormalizedDocument, config: PipelineConfig
    ) -> list[ChunkDraft]:
        return [ChunkDraft(text=document.markdown, ordinal=0)]

    assert PipelineHooks.get_document_chunker("pdf") is chunk


def test_document_chunker_falls_back_to_legacy_registration() -> None:
    received: list[str] = []

    @PipelineHooks.chunker("legacy")
    def legacy(text: str, config: PipelineConfig) -> list[str]:
        received.append(text)
        return [text]

    document = NormalizedDocument(markdown="规范 Markdown")
    drafts = PipelineHooks.get_document_chunker("legacy")(
        document, PipelineConfig()
    )

    assert received == ["规范 Markdown"]
    assert [draft.text for draft in drafts] == ["规范 Markdown"]


def test_document_chunker_snapshot_is_not_a_live_view() -> None:
    state = PipelineHooks.snapshot()

    @PipelineHooks.document_chunker("temporary")
    def chunk(
        document: NormalizedDocument, config: PipelineConfig
    ) -> list[ChunkDraft]:
        return [ChunkDraft(text=document.markdown, ordinal=0)]

    PipelineHooks.restore(state)

    # restore 后只剩默认 legacy fallback，不会拿到临时注册的函数。
    assert PipelineHooks.get_document_chunker("temporary") is not chunk


def test_hooks_state_keeps_legacy_positional_constructor() -> None:
    state = HooksState({}, {})

    assert state.async_extractors == {}
    assert state.document_chunkers == {}
