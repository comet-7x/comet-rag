"""`AsyncVectorStore` 契约：任何实现都必须通过的一套测试。

用法——实现方只需提供 `store` fixture：

    class TestInMemoryVectorStore(VectorStoreContract):
        @pytest.fixture
        async def store(self):
            return AsyncInMemoryVectorStore()

`AsyncInMemoryVectorStore` 与将来的 `AsyncMilvusStore`（T21）跑**同一套**测试。

本契约存在的头号理由是 plan R1：**Milvus 写入后不 flush/load 是查不到的**。
这个差异不体现在任何方法签名上，内存实现的测试会全绿，换 Milvus 后静默失效。
只有"写完立刻查"这类行为级用例能拦住它。

次要理由是防抽象泄漏：所有用例只用结构化 dict 过滤，绝不出现后端专有语法。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from comet_rag.ports import (
    AsyncVectorStore,
    CollectionNotFound,
    DimensionMismatch,
    VectorRecord,
)
from tests.contracts.filter_semantics import FilterSemanticsContract

DIM = 4
KB = "kb-contract"


def vec(*values: float) -> list[float]:
    """补齐到 DIM 维，省得每个用例都写一串 0。"""
    padded = list(values) + [0.0] * (DIM - len(values))
    return padded[:DIM]


def record(rid: str, text: str, embedding: list[float], **metadata) -> VectorRecord:
    return VectorRecord(id=rid, text=text, embedding=embedding, metadata=metadata)


class VectorStoreContract(FilterSemanticsContract):
    """子类必须覆写 `store` fixture。类名不以 Test 开头，故本身不被收集。"""

    @pytest.fixture
    async def store(self) -> AsyncVectorStore:  # pragma: no cover - 由子类提供
        raise NotImplementedError("实现方必须提供 store fixture")

    @pytest.fixture
    async def kb(self, store: AsyncVectorStore) -> AsyncIterator[str]:
        await store.ensure_collection(KB, dim=DIM)
        yield KB
        await store.drop_collection(KB)

    # ── 集合生命周期 ───────────────────────────────────────────────────────

    async def test_ensure_collection_is_idempotent(
        self, store: AsyncVectorStore
    ) -> None:
        """入库流程每次都会调它，不幂等就没法用。"""
        await store.ensure_collection("kb-idem", dim=DIM)
        await store.ensure_collection("kb-idem", dim=DIM)

        assert await store.count("kb-idem") == 0
        await store.drop_collection("kb-idem")

    async def test_ensure_collection_rejects_dimension_change(
        self, store: AsyncVectorStore
    ) -> None:
        """换 embedding 模型必须报错，不能悄悄接受（spec A12）。"""
        await store.ensure_collection("kb-dim", dim=DIM)

        with pytest.raises(DimensionMismatch):
            await store.ensure_collection("kb-dim", dim=DIM + 1)

        await store.drop_collection("kb-dim")

    async def test_operations_on_missing_collection_raise(
        self, store: AsyncVectorStore
    ) -> None:
        with pytest.raises(CollectionNotFound):
            await store.search("从未建过", vec(1.0))

    async def test_drop_collection_is_idempotent(self, store: AsyncVectorStore) -> None:
        await store.drop_collection("从未建过")  # 不抛即通过

    async def test_drop_collection_removes_data(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        await store.upsert(kb, [record("a", "文本", vec(1.0))])

        await store.drop_collection(kb)
        await store.ensure_collection(kb, dim=DIM)

        assert await store.count(kb) == 0

    # ── 写入与可见性（plan R1）─────────────────────────────────────────────

    async def test_upsert_is_visible_immediately(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        """**本契约最重要的一条**。

        Milvus 写入后不 flush/load 就查不到，而这个差异不体现在签名上 ——
        内存实现全绿、换 Milvus 静默失效。`upsert` 返回即必须可查。
        """
        await store.upsert(kb, [record("r1", "刚写入的内容", vec(1.0))])

        hits = await store.search(kb, vec(1.0), top_k=5)

        assert [h.id for h in hits] == ["r1"]
        assert hits[0].text == "刚写入的内容"

    async def test_upsert_overwrites_same_id(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        """重复入库同一文档必须是覆盖，不能堆副本 —— chunk id 是稳定的 SHA256。"""
        await store.upsert(kb, [record("r1", "旧内容", vec(1.0))])
        await store.upsert(kb, [record("r1", "新内容", vec(1.0))])

        assert await store.count(kb) == 1
        assert (await store.search(kb, vec(1.0)))[0].text == "新内容"

    async def test_upsert_returns_ids(self, store: AsyncVectorStore, kb: str) -> None:
        ids = await store.upsert(
            kb, [record("a", "一", vec(1.0)), record("b", "二", vec(0.0, 1.0))]
        )

        assert ids == ["a", "b"]

    async def test_upsert_empty_is_noop(self, store: AsyncVectorStore, kb: str) -> None:
        assert await store.upsert(kb, []) == []
        assert await store.count(kb) == 0

    async def test_upsert_owns_nested_metadata_and_embedding(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        tags = ["original"]
        embedding = vec(1.0)
        item = record("a", "文本", embedding, details={"tags": tags})
        await store.upsert(kb, [item])

        tags.append("changed")
        item.metadata["details"]["extra"] = True
        embedding[:] = vec(-1.0)

        hit = (await store.search(kb, vec(1.0)))[0]
        assert hit.metadata == {"details": {"tags": ["original"]}}
        assert hit.score == pytest.approx(1.0)

    async def test_search_returns_independent_nested_metadata(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        details = {"tags": ["original"]}
        await store.upsert(
            kb,
            [
                record("a", "文本", vec(1.0), details=details),
                record("b", "文本", vec(1.0), details=details),
            ],
        )

        hits = await store.search(kb, vec(1.0))
        hits[0].metadata["details"]["tags"].append("changed")
        hits[0].metadata["details"]["extra"] = True

        assert hits[1].metadata == {"details": {"tags": ["original"]}}
        fresh = await store.search(kb, vec(1.0))
        assert [hit.metadata for hit in fresh] == [
            {"details": {"tags": ["original"]}},
            {"details": {"tags": ["original"]}},
        ]

    async def test_upsert_rejects_wrong_dimension(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        with pytest.raises(DimensionMismatch):
            await store.upsert(
                kb, [VectorRecord(id="bad", text="维度不对", embedding=[1.0, 2.0])]
            )

    async def test_upsert_is_all_or_nothing_on_dimension_error(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        """一批里有一条维度不对，整批都不该写进去 —— 半写入的库最难排查。"""
        with pytest.raises(DimensionMismatch):
            await store.upsert(
                kb,
                [
                    record("ok", "正常", vec(1.0)),
                    VectorRecord(id="bad", text="维度不对", embedding=[1.0]),
                ],
            )

        assert await store.count(kb) == 0

    # ── 检索 ───────────────────────────────────────────────────────────────

    async def test_search_ranks_by_similarity(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        await store.upsert(
            kb,
            [
                record("near", "最接近", vec(1.0, 0.1)),
                record("mid", "中等", vec(0.7, 0.7)),
                record("far", "最远", vec(0.0, 1.0)),
            ],
        )

        hits = await store.search(kb, vec(1.0), top_k=3)

        assert [h.id for h in hits] == ["near", "mid", "far"]
        assert hits[0].score > hits[1].score > hits[2].score

    async def test_search_respects_top_k(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        await store.upsert(
            kb, [record(f"r{i}", f"文本{i}", vec(1.0, i * 0.1)) for i in range(10)]
        )

        assert len(await store.search(kb, vec(1.0), top_k=3)) == 3

    async def test_search_on_empty_collection_returns_empty(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        assert await store.search(kb, vec(1.0)) == []

    async def test_search_carries_metadata(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        await store.upsert(
            kb, [record("r1", "内容", vec(1.0), source="a.docx", chunk_index=3)]
        )

        hit = (await store.search(kb, vec(1.0)))[0]

        assert hit.metadata["source"] == "a.docx"
        assert hit.metadata["chunk_index"] == 3

    async def test_search_is_scoped_to_knowledge_base(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        """**跨知识库串数据是最严重的隔离事故**（spec A5 的租户边界）。"""
        await store.ensure_collection("kb-other", dim=DIM)
        await store.upsert(kb, [record("mine", "本库内容", vec(1.0))])
        await store.upsert("kb-other", [record("theirs", "别库内容", vec(1.0))])

        hits = await store.search(kb, vec(1.0), top_k=10)

        assert [h.id for h in hits] == ["mine"]
        await store.drop_collection("kb-other")

    async def test_search_rejects_wrong_query_dimension(
        self, store: AsyncVectorStore, kb: str
    ) -> None:
        with pytest.raises(DimensionMismatch):
            await store.search(kb, [1.0, 2.0])

    # ── 过滤 ───────────────────────────────────────────────────────────────

    @pytest.fixture
    async def populated(self, store: AsyncVectorStore, kb: str) -> str:
        await store.upsert(
            kb,
            [
                record("a", "甲", vec(1.0), source="a.docx", lang="zh"),
                record("b", "乙", vec(1.0), source="b.docx", lang="zh"),
                record("c", "丙", vec(1.0), source="c.docx", lang="en"),
            ],
        )
        return kb

    async def test_filter_by_equality(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        hits = await store.search(
            populated, vec(1.0), top_k=10, filter={"source": "b.docx"}
        )

        assert [h.id for h in hits] == ["b"]

    async def test_filter_by_membership(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        hits = await store.search(
            populated, vec(1.0), top_k=10, filter={"source": ["a.docx", "c.docx"]}
        )

        assert sorted(h.id for h in hits) == ["a", "c"]

    async def test_multiple_filter_keys_are_and(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        hits = await store.search(
            populated, vec(1.0), top_k=10, filter={"lang": "zh", "source": "a.docx"}
        )

        assert [h.id for h in hits] == ["a"]

    async def test_filter_matching_nothing_returns_empty(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        assert (
            await store.search(populated, vec(1.0), filter={"source": "不存在"}) == []
        )

    async def test_missing_metadata_key_does_not_match(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        """元数据里压根没有这个字段时，不该被当成匹配。"""
        assert await store.search(populated, vec(1.0), filter={"没有的字段": 1}) == []

    # ── 删除与计数 ─────────────────────────────────────────────────────────

    async def test_delete_by_ids(self, store: AsyncVectorStore, populated: str) -> None:
        deleted = await store.delete(populated, ids=["a", "b"])

        assert deleted == 2
        assert await store.count(populated) == 1

    async def test_delete_by_filter(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        """按 source 删是重新入库的前提：先清掉旧版本的全部 chunk。"""
        deleted = await store.delete(populated, filter={"lang": "zh"})

        assert deleted == 2
        assert await store.count(populated) == 1

    async def test_delete_unknown_ids_is_not_an_error(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        assert await store.delete(populated, ids=["从来没有过"]) == 0

    async def test_delete_without_criteria_is_rejected(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        """不给条件等于清空整个知识库，必须显式拒绝而不是照做。"""
        with pytest.raises(ValueError):
            await store.delete(populated)

    async def test_count_with_filter(
        self, store: AsyncVectorStore, populated: str
    ) -> None:
        assert await store.count(populated) == 3
        assert await store.count(populated, filter={"lang": "zh"}) == 2
