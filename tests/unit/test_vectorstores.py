from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import subprocess  # nosec B404
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from comet_rag.embedder.types import EmbeddingResult, EmbeddingSpace
from comet_rag.vectorstores import (
    And,
    AsyncMemoryVectorStore,
    AsyncVectorStore,
    Eq,
    In,
    VectorRecord,
)
from comet_rag.vectorstores.filters import Filter


def _space(metric: str = "COSINE") -> EmbeddingSpace:
    return EmbeddingSpace(
        model="unit", dimension=2, deployment="unit", metric=cast("Any", metric)
    )


def _store(space: EmbeddingSpace | None = None) -> AsyncMemoryVectorStore:
    return AsyncMemoryVectorStore(
        index_scope="scope",
        index_profile_id="profile",
        space=space or _space(),
        field_types={"category": str, "number": int, "active": bool, "weight": float},
    )


def _record(
    name: str,
    vector: tuple[float, ...] = (1.0, 0.0),
    *,
    space: EmbeddingSpace | None = None,
    text: str = "",
    metadata: Mapping[str, object] | None = None,
) -> VectorRecord:
    return VectorRecord(
        representation_id=name,
        unit_id=f"unit-{name}",
        source_id=f"source-{name}",
        document_id=f"document-{name}",
        index_scope="scope",
        index_profile_id="profile",
        space_id=(space or _space()).space_id,
        vector=vector,
        keyword_text=text,
        metadata=metadata or {},
    )


def _query(
    space: EmbeddingSpace | None = None, vector: tuple[float, ...] = (1.0, 0.0)
) -> EmbeddingResult:
    return EmbeddingResult("query", vector, (space or _space()).space_id)


@pytest.mark.parametrize(
    ("metric", "expected_order", "expected_scores"),
    [
        ("COSINE", ("a", "b", "c"), (1.0, 0.6, -1.0)),
        ("IP", ("b", "a", "c"), (3.0, 1.0, -1.0)),
        ("L2", ("a", "c", "b"), (0.0, 4.0, 20.0)),
    ],
)
def test_memory_exact_metrics_and_raw_direction(
    metric: str, expected_order: tuple[str, ...], expected_scores: tuple[float, ...]
) -> None:
    async def scenario() -> None:
        space = _space(metric)
        store: AsyncVectorStore = _store(space)
        await store.prepare()
        records = [
            _record("a", space=space),
            _record("b", (3.0, 4.0), space=space),
            _record("c", (-1.0, 0.0), space=space),
        ]
        confirmation = await store.upsert(records)
        assert (
            confirmation.accepted
            and confirmation.query_visible
            and not confirmation.durable
        )
        hits = await store.search(_query(space))
        assert tuple(hit.representation_id for hit in hits) == expected_order
        assert tuple(hit.score for hit in hits) == pytest.approx(expected_scores)
        assert all(
            hit.metric == metric and hit.higher_is_better == (metric != "L2")
            for hit in hits
        )
        assert (hits[0].unit_id, hits[0].source_id, hits[0].document_id) == (
            f"unit-{expected_order[0]}",
            f"source-{expected_order[0]}",
            f"document-{expected_order[0]}",
        )
        assert tuple(hit.index_scope for hit in hits) == ("scope",) * 3

    asyncio.run(scenario())


def test_memory_cosine_handles_zero_and_large_finite_vectors() -> None:
    async def scenario() -> None:
        store = _store()
        records = [_record("zero", (0.0, 0.0)), _record("large", (1e308, 1e308))]
        await store.upsert(records)
        hits = await store.search(_query(vector=(1e308, 1e308)))
        assert hits[0].score == pytest.approx(1.0)
        assert hits[1].score == 0.0

    asyncio.run(scenario())


@pytest.mark.parametrize("metric", ["IP", "L2"])
def test_memory_rejects_scores_outside_finite_range(metric: str) -> None:
    async def scenario() -> None:
        space = _space(metric)
        store = _store(space)
        await store.upsert([_record("large", (1e200, 1e200), space=space)])
        with pytest.raises(ValueError, match="finite score"):
            vector = (1e200, 1e200) if metric == "IP" else (-1e200, -1e200)
            await store.search(_query(space, vector=vector))

    asyncio.run(scenario())


def test_memory_batch_validation_conflict_idempotence_and_snapshot_isolation() -> None:
    async def scenario() -> None:
        metadata: dict[str, object] = {
            "category": "original",
            "nested": {"values": [1]},
        }
        vector = [1.0, 0.0]
        original = _record("original", cast("Any", vector), metadata=metadata)
        store = _store()
        await store.upsert([original, original])
        await store.upsert([original])
        metadata["category"] = "changed"
        vector[0] = -1.0
        object.__setattr__(original, "vector", (-1.0, 0.0))
        object.__setattr__(original, "metadata", {"category": "changed"})
        assert len(await store.search(_query())) == 1
        assert (await store.search(_query(), filters=Eq("category", "original")))[
            0
        ].score == 1.0
        good = _record("new")
        invalid = [
            replace(good, index_scope="other"),
            replace(good, index_profile_id="other"),
            replace(good, space_id=_space("IP").space_id),
            replace(good, vector=(1.0,)),
            replace(good, metadata={"number": True}),
            replace(good, metadata={"source_id": "spoofed"}),
            _record("original", (-1.0, 0.0)),
        ]
        for bad in invalid:
            with pytest.raises(ValueError):
                await store.upsert([good, bad])
            assert (await store.verify_records([good.record_id])).missing == (
                good.record_id,
            )
        with pytest.raises(ValueError):
            await store.upsert([good, replace(good, keyword_text="conflict")])
        assert (await store.verify_records([good.record_id])).missing == (
            good.record_id,
        )

    asyncio.run(scenario())


def test_memory_bm25_score_empty_text_and_filter_before_limit() -> None:
    async def scenario() -> None:
        store = _store()
        a = _record("a", text="comet comet star", metadata={"category": "excluded"})
        b = _record("b", text="comet", metadata={"category": "included"})
        empty = _record("empty", metadata={"category": "included"})
        await store.upsert([a, b, empty])
        hits = await store.search_text("COMET")
        assert {hit.record_id for hit in hits} == {a.record_id, b.record_id}
        assert all(hit.metric == "BM25" and hit.higher_is_better for hit in hits)
        expected = math.log(1 + 1.5 / 2.5) * 2.2 / (1 + 1.2 * (0.25 + 0.75 / (4 / 3)))
        assert next(
            hit.score for hit in hits if hit.record_id == b.record_id
        ) == pytest.approx(expected)
        assert [
            hit.record_id
            for hit in await store.search_text(
                "comet", limit=1, filters=Eq("category", "included")
            )
        ] == [b.record_id]
        assert await store.search_text("   ") == ()
        assert await store.search_text("missing") == ()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (Eq("source_id", "source-b"), {"b"}),
        (Eq("document_id", "document-a"), {"a"}),
        (Eq("unit_id", "unit-a"), {"a"}),
        (Eq("category", "keep"), {"b", "c"}),
        (In("category", ["keep", "drop"]), {"a", "b", "c"}),
        (In("category", []), set()),
        (And([Eq("category", "keep"), In("number", [2])]), {"b"}),
        (And([]), {"a", "b", "c", "missing"}),
        (Eq("active", True), {"b"}),
        (Eq("weight", 1.5), {"c"}),
    ],
)
def test_filters_have_same_truth_in_both_channels(
    filters: Filter, expected: set[str]
) -> None:
    async def scenario() -> None:
        store = _store()
        await store.upsert(
            [
                _record(
                    "a",
                    text="comet",
                    metadata={"category": "drop", "number": 1, "active": False},
                ),
                _record(
                    "b",
                    text="comet",
                    metadata={"category": "keep", "number": 2, "active": True},
                ),
                _record(
                    "c",
                    text="comet",
                    metadata={"category": "keep", "number": 3, "weight": 1.5},
                ),
                _record("missing", text="comet"),
            ]
        )
        assert {
            hit.representation_id
            for hit in await store.search(_query(), filters=filters)
        } == expected
        assert {
            hit.representation_id
            for hit in await store.search_text("comet", filters=filters)
        } == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "filters",
    [
        Eq("unknown", "x"),
        Eq("number", True),
        Eq("number", 1.0),
        Eq("active", 1),
        Eq("weight", 1),
        Eq("weight", float("nan")),
        Eq("category", cast("Any", None)),
        In("number", [1, True]),
        In("category", ["x", 1]),
        And([In("category", []), Eq("unknown", "x")]),
        And([Eq("category", "not-present"), Eq("number", False)]),
        And(cast("Any", [None])),
        {"category": "keep"},
    ],
)
def test_full_filter_validation_even_for_empty_store_query_and_false_clauses(
    filters: Any,
) -> None:
    async def scenario() -> None:
        store = _store()
        with pytest.raises(ValueError):
            await store.search(_query(), filters=filters)
        with pytest.raises(ValueError):
            await store.search_text("", filters=filters)

    asyncio.run(scenario())


def test_filter_configuration_freezing_and_no_metadata_identity_override() -> None:
    values = ["keep"]
    clauses: list[Filter] = [In("category", values)]
    filters = And(clauses)
    values.append("drop")
    clauses.clear()
    assert filters == And([In("category", ["keep"])])
    with pytest.raises(FrozenInstanceError):
        cast("Any", filters).clauses = ()
    for config in ({"source_id": str}, {"index_scope": str}, {"bad": list}, {"": str}):
        with pytest.raises(ValueError):
            AsyncMemoryVectorStore(
                index_scope="scope",
                index_profile_id="profile",
                space=_space(),
                field_types=config,
            )
    for constructor in (lambda: In("category", "abc"), lambda: And(cast("Any", "abc"))):
        with pytest.raises(ValueError):
            constructor()


def test_memory_verify_fingerprints_and_precise_id_deletion() -> None:
    async def scenario() -> None:
        store = _store()
        a, b, missing = _record("a"), _record("b"), _record("missing")
        await store.upsert([a, b])
        ids = [a.record_id, b.record_id, missing.record_id]
        result = await store.verify_records(
            ids,
            fingerprints={
                a.record_id: a.fingerprint,
                b.record_id: "wrong",
                missing.record_id: missing.fingerprint,
            },
        )
        assert result.present == (a.record_id,)
        assert result.unknown == (b.record_id,)
        assert result.missing == (missing.record_id,)
        confirmation = await store.delete([a.record_id, missing.record_id])
        assert (
            confirmation.accepted
            and confirmation.query_visible
            and not confirmation.durable
        )
        assert (await store.verify_records(ids)).present == (b.record_id,)
        assert (await store.verify_records(ids)).missing == (
            a.record_id,
            missing.record_id,
        )
        assert (await store.delete([])).record_ids == ()
        for invalid in ([""], [True], [b.record_id, b.record_id], "all"):
            with pytest.raises(ValueError):
                await store.delete(cast("Any", invalid))
        with pytest.raises(ValueError):
            await store.verify_records([b.record_id], fingerprints={})
        assert (await store.verify_records([b.record_id])).present == (b.record_id,)

    asyncio.run(scenario())


def test_invalid_query_and_limits_raise() -> None:
    async def scenario() -> None:
        store = _store()
        for query in (_query(_space("IP")), _query(vector=(1.0,))):
            with pytest.raises(ValueError):
                await store.search(query)
        for limit in (0, -1, True):
            with pytest.raises(ValueError):
                await store.search(_query(), limit=limit)
            with pytest.raises(ValueError):
                await store.search_text("", limit=limit)

    asyncio.run(scenario())


def test_core_vector_types_and_exports_do_not_load_optional_sdk() -> None:
    # 仅执行当前解释器与固定导入脚本。
    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-c",
            "import sys; from comet_rag.vectorstores import AsyncMemoryVectorStore, VectorRecord; from comet_rag.vectorstores.types import VectorHit; assert 'pymilvus' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class _FakeMilvusClient:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[list[dict[str, Any]]] = []
        self.upsert_failure: Exception | None = None
        self.flush_failure: Exception | None = None
        self.ack_count: int | None = None
        self.thread_ids: set[int] = set()
        self.search_results: list[list[dict[str, Any]]] = []

    def _call(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        self.thread_ids.add(threading.get_ident())

    async def query(self, collection_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        self._call("query", kwargs)
        if self.queries:
            return self.queries.pop(0)
        ids = json.loads(kwargs["filter"].split(" in ", 1)[1])
        return [dict(self.rows[key]) for key in ids if key in self.rows]

    async def upsert(
        self, collection_name: str, rows: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self._call("upsert", kwargs)
        self.rows.update({row["record_id"]: copy.deepcopy(row) for row in rows})
        if self.upsert_failure:
            raise self.upsert_failure
        return {"upsert_count": len(rows) if self.ack_count is None else self.ack_count}

    async def flush(self, collection_name: str, **kwargs: Any) -> None:
        self._call("flush", kwargs)
        if self.flush_failure:
            raise self.flush_failure

    async def delete(
        self, collection_name: str, ids: Sequence[str], **kwargs: Any
    ) -> dict[str, int]:
        self._call("delete", kwargs | {"ids": ids})
        for key in ids:
            self.rows.pop(key, None)
        return {"delete_count": len(ids)}

    async def search(
        self, collection_name: str, **kwargs: Any
    ) -> list[list[dict[str, Any]]]:
        self._call("search", kwargs)
        return self.search_results


def _milvus(client: _FakeMilvusClient) -> Any:
    from comet_rag.vectorstores.milvus import AsyncMilvusVectorStore

    store = AsyncMilvusVectorStore(
        client=cast("Any", client),
        index_scope="scope",
        index_profile_id="profile",
        space=_space(),
        field_types={"category": str, "number": int},
    )
    store._prepared = True
    return store


def test_milvus_explicit_id_verification_chunks_and_does_not_use_count() -> None:
    async def scenario() -> None:
        client = _FakeMilvusClient()
        store = _milvus(client)
        records = [_record(str(index)) for index in range(1001)]
        for row in store._rows(tuple(records)):
            client.rows[row["record_id"]] = row
        ids = tuple(record.record_id for record in records)
        result = await store.verify_records(
            ids,
            fingerprints={record.record_id: record.fingerprint for record in records},
        )
        assert result.present == ids and not result.unknown and not result.missing
        calls = [kwargs for method, kwargs in client.calls if method == "query"]
        assert [
            len(json.loads(kwargs["filter"].split(" in ", 1)[1])) for kwargs in calls
        ] == [1000, 1]
        assert all(
            kwargs["consistency_level"] == "Strong"
            and "fingerprint" in kwargs["output_fields"]
            for kwargs in calls
        )
        assert client.thread_ids == {threading.get_ident()}
        client.rows[ids[0]]["representation_id"] = "corrupted"
        client.rows[ids[1]]["index_scope"] = "other"
        client.rows[ids[2]]["fingerprint"] = _record("different").fingerprint
        result = await store.verify_records(
            ids[:3],
            fingerprints={record.record_id: record.fingerprint for record in records},
        )
        assert result.unknown == ids[:3]
        assert result.present == ()

    asyncio.run(scenario())


def test_milvus_write_validation_conflicts_and_truthful_confirmations() -> None:
    async def scenario() -> None:
        client = _FakeMilvusClient()
        store = _milvus(client)
        a, b = _record("a"), _record("b")
        confirmation = await store.upsert([a])
        assert (
            confirmation.accepted
            and confirmation.durable
            and confirmation.query_visible
        )
        await store.upsert([a])
        assert sum(method == "upsert" for method, _ in client.calls) == 1
        for records in (
            [b, replace(a, keyword_text="conflict")],
            [b, replace(b, index_profile_id="bad")],
            [b, replace(b, keyword_text="界" * 22000)],
        ):
            with pytest.raises(ValueError):
                await store.upsert(records)
            assert b.record_id not in client.rows
        client.queries = [[], []]
        confirmation = await store.upsert([b])
        assert (
            confirmation.accepted
            and confirmation.durable
            and not confirmation.query_visible
        )
        assert confirmation.unknown_ids == (b.record_id,)
        client.ack_count = 0
        confirmation = await store.upsert([_record("c")])
        assert (
            not confirmation.accepted
            and not confirmation.durable
            and not confirmation.query_visible
        )
        assert len(confirmation.unknown_ids) == 1

    asyncio.run(scenario())


def test_milvus_timeout_propagates_and_retains_possible_write() -> None:
    async def scenario() -> None:
        client = _FakeMilvusClient()
        store = _milvus(client)
        record = _record("a")
        client.upsert_failure = TimeoutError("response lost")
        with pytest.raises(TimeoutError, match="response lost"):
            await store.upsert([record])
        assert record.record_id in client.rows
        assert (
            await store.verify_records(
                [record.record_id], fingerprints={record.record_id: record.fingerprint}
            )
        ).present == (record.record_id,)
        client.flush_failure = TimeoutError("flush unknown")
        with pytest.raises(TimeoutError, match="flush unknown"):
            await store.delete([record.record_id])
        assert (await store.verify_records([record.record_id])).missing == (
            record.record_id,
        )

    asyncio.run(scenario())


def test_milvus_filters_are_escaped_native_and_checked_before_candidate_limit() -> None:
    async def scenario() -> None:
        client = _FakeMilvusClient()
        store = _milvus(client)
        value = 'quote"\\ or true or category == "anything'
        filters = And(
            [Eq("source_id", "source-a"), In("category", [value]), In("number", [])]
        )
        await store.search(_query(), limit=1, filters=filters)
        await store.search_text("comet", limit=1, filters=filters)
        searches = [kwargs for method, kwargs in client.calls if method == "search"]
        assert len(searches) == 2
        assert searches[0]["filter"] == searches[1]["filter"]
        for kwargs in searches:
            predicate = kwargs["filter"]
            assert json.dumps(value) in predicate
            assert 'source_id == "source-a"' in predicate
            assert 'record_id in [""]' in predicate
            assert 'index_scope == "scope"' in predicate
            assert kwargs["consistency_level"] == "Strong"
            assert kwargs["limit"] == 1
        for invalid in (
            Eq("unknown", "x"),
            And([In("category", []), Eq("number", True)]),
        ):
            with pytest.raises(ValueError):
                await store.search(_query(), filters=invalid)
        assert sum(method == "search" for method, _ in client.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("score", [True, "0.75", None])
def test_milvus_rejects_non_numeric_scores(score: object) -> None:
    async def scenario() -> None:
        client = _FakeMilvusClient()
        store = _milvus(client)
        record = _record("a")
        row = store._rows((record,))[0]
        client.search_results = [
            [{"record_id": record.record_id, "distance": score, "entity": row}]
        ]
        for operation in (store.search(_query()), store.search_text("comet")):
            with pytest.raises(RuntimeError, match="score"):
                await operation

    asyncio.run(scenario())


def test_milvus_rejects_duplicate_hits() -> None:
    async def scenario() -> None:
        client = _FakeMilvusClient()
        store = _milvus(client)
        record = _record("a")
        row = store._rows((record,))[0]
        hit = {"record_id": record.record_id, "distance": 0.75, "entity": row}
        client.search_results = [[hit, hit]]
        for operation in (store.search(_query()), store.search_text("comet")):
            with pytest.raises(RuntimeError, match="duplicate"):
                await operation

    asyncio.run(scenario())


def test_milvus_named_primary_key_hits_and_inconsistent_responses() -> None:
    async def scenario() -> None:
        client = _FakeMilvusClient()
        store = _milvus(client)
        record = _record("a")
        row = store._rows((record,))[0]
        client.search_results = [
            [{"record_id": record.record_id, "distance": 0.75, "entity": row}]
        ]
        hits = await store.search(_query())
        assert len(hits) == 1 and hits[0].record_id == record.record_id
        assert hits[0].score == 0.75 and hits[0].metric == "COSINE"
        client.search_results[0][0]["entity"] = row | {"index_profile_id": "other"}
        with pytest.raises(RuntimeError, match="inconsistent"):
            await store.search_text("comet")
        client.search_results[0][0]["entity"] = row
        client.search_results[0][0]["distance"] = float("nan")
        with pytest.raises(RuntimeError, match="nonfinite"):
            await store.search(_query())
        client.queries = [[row, row]]
        assert (await store.verify_records([record.record_id])).unknown == (
            record.record_id,
        )
        client.queries = [[row | {"record_id": "unexpected"}]]
        with pytest.raises(RuntimeError, match="unexpected"):
            await store.verify_records([record.record_id])

    asyncio.run(scenario())


class _PreparingMilvusClient(_FakeMilvusClient):
    def __init__(self) -> None:
        super().__init__()
        self.description: dict[str, Any] | None = None
        self.indexes: dict[str, dict[str, Any]] = {}
        self.load_failure: Exception | None = None
        self.index_failure: Exception | None = None
        self.loaded = False

    async def has_collection(self, name: str, **kwargs: Any) -> bool:
        self._call("has_collection", kwargs)
        return self.description is not None

    def create_schema(self, **kwargs: Any) -> Any:
        from pymilvus import AsyncMilvusClient

        return AsyncMilvusClient.create_schema(**kwargs)

    async def create_collection(self, name: str, *, schema: Any, **kwargs: Any) -> None:
        self._call("create_collection", kwargs)
        self.description = schema.to_dict()

    async def describe_collection(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self._call("describe_collection", kwargs)
        assert self.description is not None
        return copy.deepcopy(self.description)

    def prepare_index_params(self) -> Any:
        from pymilvus import AsyncMilvusClient

        return AsyncMilvusClient.prepare_index_params()

    async def create_index(self, name: str, parameters: Any, **kwargs: Any) -> None:
        self._call("create_index", kwargs)
        if self.index_failure:
            raise self.index_failure
        for index in parameters:
            description = index.to_dict()
            self.indexes[description["index_name"]] = description | {
                "state": "Finished"
            }

    async def list_indexes(self, name: str, **kwargs: Any) -> list[str]:
        self._call("list_indexes", kwargs)
        return list(self.indexes)

    async def describe_index(
        self, name: str, index_name: str, **kwargs: Any
    ) -> dict[str, Any]:
        self._call("describe_index", kwargs)
        return dict(self.indexes[index_name])

    async def load_collection(self, name: str, **kwargs: Any) -> None:
        self._call("load_collection", kwargs)
        if self.load_failure:
            raise self.load_failure
        self.loaded = True

    async def get_load_state(self, name: str, **kwargs: Any) -> dict[str, Any]:
        from pymilvus.client.types import LoadState

        self._call("get_load_state", kwargs)
        return {"state": LoadState.Loaded if self.loaded else LoadState.NotLoad}


def test_milvus_awaits_native_io_and_keeps_client_ownership(monkeypatch) -> None:
    async def scenario() -> None:
        client = _PreparingMilvusClient()
        methods = (
            "has_collection",
            "create_collection",
            "describe_collection",
            "list_indexes",
            "describe_index",
            "create_index",
            "load_collection",
            "get_load_state",
            "query",
            "upsert",
            "flush",
            "search",
            "delete",
        )
        calls = {}
        for method in methods:
            call = AsyncMock(wraps=getattr(client, method))
            monkeypatch.setattr(client, method, call)
            calls[method] = call
        close = AsyncMock()
        monkeypatch.setattr(client, "close", close, raising=False)
        store = _milvus(client)
        await store.prepare()
        await store.prepare()
        record = _record("a")
        await store.upsert([record])
        await store.search(_query())
        await store.search_text("comet")
        assert (await store.verify_records([record.record_id])).present == (
            record.record_id,
        )
        assert (await store.delete([record.record_id])).query_visible
        for call in calls.values():
            assert call.await_count == call.call_count > 0
        close.assert_not_called()
        assert client.thread_ids == {threading.get_ident()}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("operation", "rpc"),
    [
        ("prepare", "has_collection"),
        ("upsert", "upsert"),
        ("search", "search"),
        ("search_text", "search"),
        ("verify_records", "query"),
        ("delete", "delete"),
    ],
)
def test_milvus_cancellation_reaches_native_io_without_followup_or_cleanup(
    operation: str, rpc: str, monkeypatch
) -> None:
    async def scenario() -> None:
        client = _PreparingMilvusClient()
        store = _milvus(client)
        await store.prepare()
        record = _record("a")
        if operation == "delete":
            await store.upsert([record])
        started, cancelled = asyncio.Event(), asyncio.Event()
        original = getattr(client, rpc)

        async def blocked(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return result

        call = AsyncMock(side_effect=blocked)
        monkeypatch.setattr(client, rpc, call)
        arguments = {
            "prepare": (),
            "upsert": ([record],),
            "search": (_query(),),
            "search_text": ("comet",),
            "verify_records": ([record.record_id],),
            "delete": ([record.record_id],),
        }
        task = asyncio.create_task(getattr(store, operation)(*arguments[operation]))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        call.assert_awaited_once()
        assert client.calls[-1][0] == rpc
        if operation == "prepare":
            assert store._prepared
            await store.search(_query())
        if operation == "upsert":
            assert record.record_id in client.rows
        if operation == "delete":
            assert record.record_id not in client.rows

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "corruption",
    [
        "description",
        "missing_field",
        "auto_id",
        "dynamic",
        "nullable",
        "primary",
        "dimension",
        "length",
        "analyzer",
        "function",
        "function_fields",
        "dense_metric",
        "sparse_metric",
    ],
)
def test_milvus_prepare_rejects_incompatible_schema_and_indexes_without_mutation(
    corruption: str,
) -> None:
    async def scenario() -> None:
        client = _PreparingMilvusClient()
        store = _milvus(client)
        await store.prepare()
        assert client.description is not None
        fields = {field["name"]: field for field in client.description["fields"]}
        if corruption == "description":
            client.description["description"] = "other-profile"
        elif corruption == "missing_field":
            client.description["fields"].remove(fields["fingerprint"])
        elif corruption == "auto_id":
            client.description["auto_id"] = True
        elif corruption == "dynamic":
            client.description["enable_dynamic_field"] = True
        elif corruption == "nullable":
            fields["metadata"]["nullable"] = True
        elif corruption == "primary":
            fields["record_id"]["is_primary"] = False
        elif corruption == "dimension":
            fields["dense"]["params"]["dim"] = 3
        elif corruption == "length":
            fields["source_id"]["params"]["max_length"] = 100
        elif corruption == "analyzer":
            fields["keyword_text"]["params"]["analyzer_params"] = {"type": "chinese"}
        elif corruption == "function":
            client.description["functions"] = []
        elif corruption == "function_fields":
            client.description["functions"][0]["input_field_names"] = ["metadata"]
        elif corruption == "dense_metric":
            client.indexes["dense_index"]["metric_type"] = "IP"
        else:
            client.indexes["keyword_index"]["metric_type"] = "IP"
        original = copy.deepcopy((client.description, client.indexes))
        client.calls.clear()
        with pytest.raises(ValueError, match="incompatible"):
            await store.prepare()
        assert (client.description, client.indexes) == original
        assert all(
            method
            not in (
                "create_index",
                "create_collection",
                "load_collection",
                "delete",
                "upsert",
            )
            for method, _ in client.calls
        )
        with pytest.raises(RuntimeError, match="prepare"):
            await store.search(_query())

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "cancel", "timeout"])
def test_repeated_prepare_preserves_concurrent_search_readiness(outcome: str) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class Client(_PreparingMilvusClient):
            hold = False

            async def has_collection(self, name: str, **kwargs: Any) -> bool:
                if self.hold:
                    entered.set()
                    await release.wait()
                    if outcome == "timeout":
                        raise TimeoutError("transient preparation failure")
                return await super().has_collection(name, **kwargs)

        client = Client()
        store = _milvus(client)
        await store.prepare()
        await store.search(_query())
        client.hold = True
        pending = asyncio.create_task(store.prepare())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await store.search(_query())
            await store.search_text("cat")
            if outcome == "cancel":
                pending.cancel()
            release.set()
            if outcome == "success":
                await pending
            else:
                with pytest.raises(
                    asyncio.CancelledError if outcome == "cancel" else TimeoutError
                ):
                    await pending
            await store.search(_query())
            await store.search_text("cat")
            assert client.loaded
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


def test_milvus_prepare_resumes_after_index_or_load_failure_without_drop() -> None:
    async def scenario() -> None:
        client = _PreparingMilvusClient()
        store = _milvus(client)
        client.index_failure = TimeoutError("index uncertain")
        with pytest.raises(TimeoutError, match="index uncertain"):
            await store.prepare()
        assert client.description is not None and not client.indexes
        client.index_failure = None
        client.load_failure = TimeoutError("load uncertain")
        with pytest.raises(TimeoutError, match="load uncertain"):
            await store.prepare()
        assert set(client.indexes) == {"dense_index", "keyword_index"}
        client.load_failure = None
        await store.prepare()
        await store.prepare()
        assert sum(method == "create_collection" for method, _ in client.calls) == 1
        assert sum(method == "create_index" for method, _ in client.calls) == 2
        assert client.thread_ids == {threading.get_ident()}

    asyncio.run(scenario())


def test_milvus_prepare_requires_data_query_after_load(monkeypatch) -> None:
    from pymilvus import MilvusException

    from comet_rag.vectorstores import milvus

    class Recovering(_PreparingMilvusClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def query(
            self, collection_name: str, **kwargs: Any
        ) -> list[dict[str, Any]]:
            self.attempts += 1
            if self.attempts == 1:
                raise MilvusException(code=503, message="channel not serviceable")
            return await super().query(collection_name, **kwargs)

    async def scenario() -> None:
        client = Recovering()
        store = _milvus(client)
        pause = AsyncMock()
        monkeypatch.setattr(milvus.asyncio, "sleep", pause)
        await store.prepare()
        assert client.attempts == 2 and store._prepared
        pause.assert_awaited_once()
        client.attempts = 0
        store._timeout = 0.000001
        with pytest.raises(MilvusException):
            await store.prepare()
        assert not store._prepared

    asyncio.run(scenario())


def test_collection_names_are_stable_scoped_and_valid() -> None:
    from comet_rag.vectorstores.milvus import collection_name_for

    scopes = ["a-b", "a_b", "中文 scope", "x" * 1024]
    names = [collection_name_for(scope) for scope in scopes]
    assert names == [collection_name_for(scope) for scope in scopes]
    assert len(set(names)) == len(scopes)
    assert all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and len(name) < 255
        for name in names
    )
