"""相同手写真值表覆盖两种存储的所有过滤读写路径。"""

from __future__ import annotations

from typing import cast

import pytest

from comet_rag.ports import (
    AsyncKeywordSearchPort,
    AsyncVectorStore,
    Filter,
    VectorRecord,
)

ROWS = {
    "true": {"v": True},
    "false": {"v": False},
    "one": {"v": 1},
    "float-one": {"v": 1.0},
    "zero": {"v": 0},
    "string-one": {"v": "1"},
    "empty-string": {"v": ""},
    "null": {"v": None},
    "missing": {},
    "array": {"v": [1]},
    "object": {"v": {"nested": 1}},
    "big": {"v": 2**53 + 1},
}

# 预期独立手写，不从 normalize_filter/matches_filter 或查询结果生成。
CASES = [
    pytest.param({"v": True}, ["true"], id="bool-true"),
    pytest.param({"v": False}, ["false"], id="bool-false"),
    pytest.param({"v": 1}, ["float-one", "one"], id="int"),
    pytest.param({"v": 1.0}, ["float-one", "one"], id="float"),
    pytest.param({"v": 0}, ["zero"], id="zero-is-not-false"),
    pytest.param({"v": "1"}, ["string-one"], id="no-string-coercion"),
    pytest.param({"v": ""}, ["empty-string"], id="empty-string-not-null"),
    pytest.param({"v": [1]}, ["float-one", "one"], id="numeric-in"),
    pytest.param({"v": [1.0]}, ["float-one", "one"], id="float-in"),
    pytest.param({"v": [True]}, ["true"], id="bool-in"),
    pytest.param({"v": []}, [], id="empty-in"),
    pytest.param({"absent": "x"}, [], id="missing-never-matches"),
    pytest.param({"v": 1, "absent": "x"}, [], id="and"),
    pytest.param(
        {},
        [
            "array",
            "big",
            "empty-string",
            "false",
            "float-one",
            "missing",
            "null",
            "object",
            "one",
            "string-one",
            "true",
            "zero",
        ],
        id="empty-filter",
    ),
]


class FilterSemanticsContract:
    @pytest.mark.parametrize("operation", ["dense", "keyword", "count", "delete"])
    async def test_invalid_filter_does_not_depend_on_existing_matches(
        self, store: AsyncVectorStore, kb: str, operation: str
    ) -> None:
        await store.upsert(
            kb, [VectorRecord("kept", "shared keyword", [1.0, 0.0, 0.0, 0.0], {"v": 1})]
        )
        condition = cast("Filter", {"absent": "no-match", "bad": None})
        with pytest.raises(ValueError):
            if operation == "dense":
                await store.search(kb, [1.0, 0.0, 0.0, 0.0], filter=condition)
            elif operation == "keyword":
                assert isinstance(store, AsyncKeywordSearchPort)
                await store.search_keywords(kb, " ", filter=condition)
            elif operation == "count":
                await store.count(kb, filter=condition)
            else:
                await store.delete(kb, filter=condition)
        assert await store.count(kb) == 1

    @pytest.mark.parametrize("condition,expected", CASES)
    async def test_filter_truth_table_across_read_and_delete(
        self, store: AsyncVectorStore, kb: str, condition: Filter, expected: list[str]
    ) -> None:
        await store.upsert(
            kb,
            [
                VectorRecord(rid, "shared keyword", [1.0, 0.0, 0.0, 0.0], metadata)
                for rid, metadata in ROWS.items()
            ],
        )
        assert await store.count(kb, filter=condition) == len(expected)
        assert (
            sorted(
                hit.id
                for hit in await store.search(
                    kb, [1.0, 0.0, 0.0, 0.0], top_k=32, filter=condition
                )
            )
            == expected
        )
        assert isinstance(store, AsyncKeywordSearchPort)
        assert (
            sorted(
                hit.id
                for hit in await store.search_keywords(
                    kb, "keyword", top_k=32, filter=condition
                )
            )
            == expected
        )
        if not condition:
            # 无过滤可读取全库；删除仍需独立的显式范围守卫。
            with pytest.raises(ValueError, match="非空 Filter"):
                await store.delete(kb, filter=condition)
            assert await store.count(kb) == len(ROWS)
            return
        assert await store.delete(kb, filter=condition) == len(expected)
        assert await store.count(kb) == len(ROWS) - len(expected)
