"""`KeywordSearchPort` 的后端无关行为契约。

契约只约束匹配、隔离、过滤和排序等语义，不约束跨实现分数相等。内存实现使用
轻量 tokenizer，Milvus 使用 chinese analyzer；尤其 CJK 分词与 BM25 分数会不同。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

import pytest

from comet_rag.ports import (
    CollectionNotFound,
    Filter,
    KeywordSearchPort,
    VectorRecord,
)

DIM = 4
KB = "kb-keyword-contract"


class KeywordSearchStore(KeywordSearchPort, Protocol):
    """契约准备数据所需的最小生命周期能力。"""

    async def aensure_collection(self, kb_id: str, *, dim: int) -> None: ...

    async def aupsert(
        self, kb_id: str, records: Sequence[VectorRecord]
    ) -> list[str]: ...

    async def adrop_collection(self, kb_id: str) -> None: ...


def keyword_records() -> list[VectorRecord]:
    return [
        VectorRecord(
            id="zh",
            text="星际量子纠缠校准指南",
            embedding=[1.0, 0.0, 0.0, 0.0],
            metadata={"language": "zh", "source": "physics.docx"},
        ),
        VectorRecord(
            id="en",
            text="Rust TraitObject memory layout",
            embedding=[0.0, 1.0, 0.0, 0.0],
            metadata={"language": "en", "source": "rust.md"},
        ),
        VectorRecord(
            id="other",
            text="香蕉适合在热带地区种植",
            embedding=[0.0, 0.0, 1.0, 0.0],
            metadata={"language": "zh", "source": "fruit.txt"},
        ),
    ]


class KeywordSearchContract:
    """内存和生产适配器必须共同满足的关键词检索语义。"""

    @pytest.fixture
    async def store(self) -> KeywordSearchStore:  # pragma: no cover
        raise NotImplementedError("实现方必须提供 store fixture")

    @pytest.fixture
    async def kb(self, store: KeywordSearchStore) -> AsyncIterator[str]:
        await store.aensure_collection(KB, dim=DIM)
        yield KB
        await store.adrop_collection(KB)

    @pytest.fixture
    async def populated(self, store: KeywordSearchStore, kb: str) -> str:
        await store.aupsert(kb, keyword_records())
        return kb

    async def test_chinese_term_matches_expected_document(
        self, store: KeywordSearchStore, populated: str
    ) -> None:
        hits = await store.asearch_keywords(populated, "量子纠缠", top_k=3)

        assert [hit.id for hit in hits] == ["zh"]
        assert hits[0].score > 0

    async def test_english_identifier_matches_expected_document(
        self, store: KeywordSearchStore, populated: str
    ) -> None:
        hits = await store.asearch_keywords(populated, "TraitObject", top_k=3)

        assert [hit.id for hit in hits] == ["en"]

    async def test_no_matching_term_returns_empty(
        self, store: KeywordSearchStore, populated: str
    ) -> None:
        assert await store.asearch_keywords(populated, "OpenTelemetry") == []

    async def test_whitespace_query_returns_empty_after_collection_check(
        self, store: KeywordSearchStore, kb: str
    ) -> None:
        assert await store.asearch_keywords(kb, " \n\t ") == []

    async def test_missing_collection_is_not_treated_as_empty(
        self, store: KeywordSearchStore
    ) -> None:
        with pytest.raises(CollectionNotFound):
            await store.asearch_keywords("missing-keyword-kb", "量子")

    async def test_top_k_is_enforced(
        self, store: KeywordSearchStore, kb: str
    ) -> None:
        await store.aupsert(
            kb,
            [
                VectorRecord(
                    id=f"r{index}",
                    text=f"共同检索术语 文档{index}",
                    embedding=[1.0, 0.0, 0.0, 0.0],
                )
                for index in range(4)
            ],
        )

        assert len(await store.asearch_keywords(kb, "检索术语", top_k=2)) == 2

    @pytest.mark.parametrize("top_k", [0, -1])
    async def test_non_positive_top_k_is_rejected(
        self, store: KeywordSearchStore, kb: str, top_k: int
    ) -> None:
        with pytest.raises(ValueError, match="top_k"):
            await store.asearch_keywords(kb, "量子", top_k=top_k)

    async def test_equal_scores_are_ordered_by_id(
        self, store: KeywordSearchStore, kb: str
    ) -> None:
        await store.aupsert(
            kb,
            [
                VectorRecord(
                    id="b",
                    text="完全相同术语",
                    embedding=[1.0, 0.0, 0.0, 0.0],
                ),
                VectorRecord(
                    id="a",
                    text="完全相同术语",
                    embedding=[0.0, 1.0, 0.0, 0.0],
                ),
            ],
        )

        hits = await store.asearch_keywords(kb, "完全相同术语", top_k=2)

        assert [hit.id for hit in hits] == ["a", "b"]
        assert hits[0].score == pytest.approx(hits[1].score)

    async def test_filter_by_equality(
        self, store: KeywordSearchStore, populated: str
    ) -> None:
        hits = await store.asearch_keywords(
            populated, "Rust TraitObject", filter={"language": "zh"}
        )

        assert hits == []

    async def test_filter_by_membership_and_metadata_are_preserved(
        self, store: KeywordSearchStore, populated: str
    ) -> None:
        filter: Filter = {"source": ["rust.md", "physics.docx"]}
        hits = await store.asearch_keywords(
            populated, "TraitObject", filter=filter
        )

        assert [hit.id for hit in hits] == ["en"]
        assert hits[0].text == "Rust TraitObject memory layout"
        assert hits[0].metadata == {"language": "en", "source": "rust.md"}

    async def test_search_is_scoped_to_knowledge_base(
        self, store: KeywordSearchStore, populated: str
    ) -> None:
        other = "kb-keyword-other"
        await store.aensure_collection(other, dim=DIM)
        try:
            await store.aupsert(
                other,
                [
                    VectorRecord(
                        id="foreign",
                        text="量子纠缠属于其他知识库",
                        embedding=[1.0, 0.0, 0.0, 0.0],
                    )
                ],
            )

            hits = await store.asearch_keywords(populated, "量子纠缠")

            assert [hit.id for hit in hits] == ["zh"]
        finally:
            await store.adrop_collection(other)


__all__ = [
    "KeywordSearchContract",
    "KeywordSearchStore",
    "keyword_records",
]
