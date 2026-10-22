"""Milvus BM25 schema v2 与数据库隔离测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pymilvus import AsyncMilvusClient, DataType, Function, FunctionType
from pymilvus.client.connection_manager import AsyncConnectionManager
from pymilvus.client.types import LoadState

from comet_rag.infrastructure.persistence.vector_store import milvus as target
from comet_rag.ports import CollectionNotFound, CollectionSchemaMismatch, VectorRecord


def _v2_schema(*, dim: int = 4) -> dict[str, Any]:
    schema = AsyncMilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=65535,
        enable_analyzer=True,
        analyzer_params={"type": "chinese"},
    )
    schema.add_field("metadata", DataType.JSON)
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(
        Function(
            name="text_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse_vector"],
        )
    )
    return schema.to_dict()


def _v2_index() -> dict[str, Any]:
    return {
        "field_name": "sparse_vector",
        "index_type": "SPARSE_INVERTED_INDEX",
        "metric_type": "BM25",
    }


def test_v2_schema_is_accepted() -> None:
    assert target._schema_mismatches(_v2_schema(), [_v2_index()]) == []  # noqa: SLF001


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda schema: schema["fields"][1]["params"].pop("enable_analyzer"),
            "未启用 analyzer",
        ),
        (
            lambda schema: schema["fields"][1]["params"].update(
                {"analyzer_params": '{"type":"standard"}'}
            ),
            "不是 chinese",
        ),
        (lambda schema: schema["fields"].pop(), "缺少 sparse_vector 字段"),
        (lambda schema: schema.update({"functions": []}), "BM25 function"),
    ],
)
def test_schema_checker_rejects_each_required_bm25_component(
    mutate: Any, expected: str
) -> None:
    schema = _v2_schema()
    mutate(schema)

    assert expected in "；".join(  # noqa: SLF001
        target._schema_mismatches(schema, [_v2_index()])
    )


def test_schema_checker_rejects_old_ip_sparse_index() -> None:
    index = _v2_index() | {"metric_type": "IP"}

    assert "BM25 SPARSE_INVERTED_INDEX" in "；".join(  # noqa: SLF001
        target._schema_mismatches(_v2_schema(), [index])
    )


class _FakeClient:
    def __init__(
        self,
        *,
        exists: bool,
        description: dict[str, Any] | None = None,
        fail_index: bool = False,
        index_ready_after: int = 0,
        describe_index_none: bool = False,
        create_conflict: bool = False,
        block_load: bool = False,
        fail_load: bool = False,
        delete_on_load: bool = False,
    ):
        self.exists = exists
        self.description = description or _v2_schema()
        self.fail_index = fail_index
        self.index_ready_after = index_ready_after
        self.describe_index_none = describe_index_none
        self.create_conflict = create_conflict
        self.block_load = block_load
        self.fail_load = fail_load
        self.delete_on_load = delete_on_load
        self.list_index_calls = 0
        self.load_calls = 0
        self.load_started = asyncio.Event()
        self.both_loading = asyncio.Event()
        self.allow_load = asyncio.Event()
        self.load_cancelled = asyncio.Event()
        self.load_state = LoadState.Loaded if exists else LoadState.NotExist
        self.created: dict[str, Any] | None = None
        self.indexes: Any = None
        self.loaded: dict[str, Any] | None = None
        self.dropped = False
        self.rows: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.closed = False

    async def has_collection(self, name: str) -> bool:
        return self.exists

    async def describe_collection(self, name: str) -> dict[str, Any]:
        return self.description

    async def list_indexes(self, name: str, *, field_name: str) -> list[str]:
        self.list_index_calls += 1
        if self.list_index_calls <= self.index_ready_after:
            return []
        return ["sparse_vector"]

    async def describe_index(self, name: str, index_name: str) -> dict[str, Any] | None:
        if self.describe_index_none:
            return None
        return _v2_index()

    def create_schema(self, **kwargs: Any):
        return AsyncMilvusClient.create_schema(**kwargs)

    def prepare_index_params(self):
        return AsyncMilvusClient.prepare_index_params()

    async def create_collection(self, name: str, **kwargs: Any) -> None:
        if self.create_conflict:
            self.exists = True
            self.load_state = LoadState.Loaded
            raise RuntimeError("collection already exists")
        self.created = {"name": name, **kwargs}
        self.exists = True
        self.load_state = LoadState.NotLoad

    async def create_index(self, name: str, *, index_params: Any) -> None:
        if self.fail_index:
            raise RuntimeError("index failed")
        self.indexes = index_params

    async def load_collection(self, name: str, **kwargs: Any) -> None:
        self.load_calls += 1
        self.load_state = LoadState.Loading
        self.load_started.set()
        if self.load_calls == 2:
            self.both_loading.set()
        try:
            if self.block_load:
                await asyncio.wait_for(self.allow_load.wait(), timeout=2)
        except asyncio.CancelledError:
            self.load_cancelled.set()
            raise
        if self.fail_load:
            raise RuntimeError("load failed")
        if self.delete_on_load:
            self.exists = False
            self.load_state = LoadState.NotExist
            return
        self.load_state = LoadState.Loaded
        self.loaded = {"name": name, **kwargs}

    async def get_load_state(self, name: str) -> dict[str, LoadState]:
        return {"state": self.load_state}

    async def drop_collection(self, name: str) -> None:
        self.dropped = True
        self.exists = False
        self.load_state = LoadState.NotExist

    async def upsert(self, name: str, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def search(self, name: str, **kwargs: Any) -> list:
        self.reads.append(kwargs)
        return [[]]

    async def query(self, name: str, **kwargs: Any) -> list:
        self.reads.append(kwargs)
        if kwargs.get("output_fields") == ["id"]:
            return [{"id": row["id"]} for row in self.rows]
        return [{"count(*)": 0}]

    async def delete(self, name: str, *, ids: list[str]) -> None:
        self.rows = [row for row in self.rows if row["id"] not in ids]

    async def close(self) -> None:
        self.closed = True


def _store(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
    *,
    consistency_level: str = "Session",
) -> tuple[target.AsyncMilvusStore, _FakeClient, list[dict[str, Any]]]:
    constructor_args: list[dict[str, Any]] = []

    def async_factory(**kwargs: Any) -> _FakeClient:
        constructor_args.append(kwargs)
        return client

    monkeypatch.setattr(target, "AsyncMilvusClient", async_factory)
    store = target.AsyncMilvusStore(
        endpoint="http://milvus.invalid:19530",
        database_name="zhihao_test_database",
        prefix="ct_schema",
        consistency_level=consistency_level,
    )
    return store, client, constructor_args


@pytest.mark.parametrize("consistency", ["Session", "Strong"])
async def test_existing_collection_reads_explicitly_use_adapter_consistency(
    monkeypatch: pytest.MonkeyPatch,
    consistency: str,
) -> None:
    store, client, _ = _store(
        monkeypatch, _FakeClient(exists=True), consistency_level=consistency
    )
    await store.search("existing-kb", [1.0, 0.0, 0.0, 0.0])
    await store.search_keywords("existing-kb", "量子纠缠")
    await store.count("existing-kb", filter={"document_revision": "new"})
    assert [call["consistency_level"] for call in client.reads] == [consistency] * 3
    assert client.reads[2]["filter"] == 'metadata["document_revision"] == "new"'


async def test_new_collection_uses_bm25_schema_and_server_generated_sparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=False)
    store, _, constructors = _store(monkeypatch, client)

    await store.ensure_collection("kb", dim=4)
    await store.upsert(
        "kb", [VectorRecord(id="r1", text="中文 BM25", embedding=[1, 0, 0, 0])]
    )

    assert constructors == [
        {
            "uri": "http://milvus.invalid:19530",
            "token": "",
            "db_name": "zhihao_test_database",
        }
    ]
    assert client.created is not None
    schema = client.created["schema"].to_dict()
    indexes = [item.to_dict() for item in client.indexes]
    assert target._schema_mismatches(schema, indexes) == []  # noqa: SLF001
    assert client.loaded is not None
    assert client.loaded["replica_number"] == 1
    assert "sparse_vector" not in client.rows[0]


async def test_old_schema_is_rejected_without_create_or_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_schema = _v2_schema()
    old_schema["functions"] = []
    client = _FakeClient(exists=True, description=old_schema)
    store, _, _ = _store(monkeypatch, client)

    with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
        await store.ensure_collection("existing-kb", dim=4)

    assert client.created is None
    assert client.dropped is False


async def test_ensure_waits_for_concurrent_creator_to_finish_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=True, index_ready_after=2)
    store, _, _ = _store(monkeypatch, client)
    monkeypatch.setattr(target, "_SCHEMA_READY_INTERVAL_SECONDS", 0)

    await store.ensure_collection("shared-kb", dim=4)

    assert client.list_index_calls == 3
    assert store._dims["shared-kb"] == 4  # noqa: SLF001


async def test_ensure_recovers_when_create_loses_cross_process_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=False, create_conflict=True)
    store, _, _ = _store(monkeypatch, client)

    await store.ensure_collection("shared-kb", dim=4)

    assert store._dims["shared-kb"] == 4  # noqa: SLF001
    assert "shared-kb" not in store._created  # noqa: SLF001


async def test_two_instances_wait_until_collection_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=False, block_load=True)
    first, _, _ = _store(monkeypatch, client)
    second, _, _ = _store(monkeypatch, client)

    first_task = asyncio.create_task(first.ensure_collection("shared-kb", dim=4))
    tasks = [first_task]
    try:
        await asyncio.wait_for(client.load_started.wait(), 1)
        second_task = asyncio.create_task(second.ensure_collection("shared-kb", dim=4))
        tasks.append(second_task)
        await asyncio.wait_for(client.both_loading.wait(), 1)
        assert not first_task.done()
        assert not second_task.done()
    finally:
        client.allow_load.set()
        await asyncio.gather(*tasks)

    assert client.load_state == LoadState.Loaded
    assert first._dims["shared-kb"] == 4  # noqa: SLF001
    assert second._dims["shared-kb"] == 4  # noqa: SLF001


async def test_load_failure_keeps_collection_and_does_not_cache_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=False, fail_load=True)
    store, _, _ = _store(monkeypatch, client)

    with pytest.raises(RuntimeError, match="load failed"):
        await store.ensure_collection("retryable-kb", dim=4)

    assert client.exists is True
    assert client.dropped is False
    assert "retryable-kb" not in store._dims  # noqa: SLF001

    client.fail_load = False
    await store.ensure_collection("retryable-kb", dim=4)
    assert store._dims["retryable-kb"] == 4  # noqa: SLF001


async def test_collection_deleted_during_load_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=False, delete_on_load=True)
    store, _, _ = _store(monkeypatch, client)

    with pytest.raises(CollectionNotFound):
        await store.ensure_collection("deleted-kb", dim=4)

    assert "deleted-kb" not in store._dims  # noqa: SLF001


async def test_missing_index_description_becomes_stable_schema_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=True, describe_index_none=True)
    store, _, _ = _store(monkeypatch, client)
    monkeypatch.setattr(target, "_SCHEMA_READY_ATTEMPTS", 1)

    with pytest.raises(CollectionSchemaMismatch, match="SPARSE_INVERTED_INDEX"):
        await store.ensure_collection("existing-kb", dim=4)


@pytest.mark.parametrize("mode", ["dense", "keyword"])
async def test_direct_search_after_restart_rejects_old_schema(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """进程重启后的空缓存不能让读路径绕过 schema v2 校验。"""
    old_schema = _v2_schema()
    old_schema["functions"] = []
    client = _FakeClient(exists=True, description=old_schema)
    store, _, _ = _store(monkeypatch, client)

    with pytest.raises(CollectionSchemaMismatch, match="BM25 function"):
        if mode == "dense":
            await store.search("existing-kb", [1.0, 0.0, 0.0, 0.0])
        else:
            await store.search_keywords("existing-kb", "量子")

    assert store._dims == {}  # noqa: SLF001
    assert client.dropped is False


async def test_new_collection_is_rolled_back_when_index_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=False, fail_index=True)
    store, _, _ = _store(monkeypatch, client)

    with pytest.raises(RuntimeError, match="index failed"):
        await store.ensure_collection("new-kb", dim=4)

    assert client.dropped is True


def test_database_name_is_required_before_clients_are_created() -> None:
    with pytest.raises(ValueError, match="database_name"):
        target.AsyncMilvusStore(database_name="   ")


async def test_native_sdk_construction_and_unused_close_do_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_connection(*args: object, **kwargs: object) -> None:
        pytest.fail("构造和关闭未使用的客户端不应触发连接")

    monkeypatch.setattr(AsyncConnectionManager, "get_instance", unexpected_connection)
    monkeypatch.setattr(target, "MilvusClient", unexpected_connection, raising=False)

    store = target.AsyncMilvusStore(database_name="lazy")
    await store.close()


async def test_first_operation_preserves_native_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError("Milvus unavailable")

    async def unavailable(name: str) -> bool:
        raise failure

    client = _FakeClient(exists=False)
    monkeypatch.setattr(client, "has_collection", unavailable)
    store, _, _ = _store(monkeypatch, client)

    with pytest.raises(OSError) as caught:
        await store.ensure_collection("kb", dim=4)

    assert caught.value is failure
    assert client.created is None
    assert store._dims == {}  # noqa: SLF001
    await store.close()
    assert client.closed


async def test_milvus_operations_use_native_async_without_thread_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_thread(*args: object, **kwargs: object) -> None:
        pytest.fail("Milvus 已有原生异步方法，不应通过线程桥调用")

    monkeypatch.setattr(asyncio, "to_thread", forbidden_thread)
    client = _FakeClient(exists=False)
    store, _, _ = _store(monkeypatch, client)
    await store.ensure_collection("kb", dim=4)
    await store.upsert("kb", [VectorRecord("r1", "text", [1.0, 0.0, 0.0, 0.0])])
    await store.search("kb", [1.0, 0.0, 0.0, 0.0])
    await store.search_keywords("kb", "text")
    await store.count("kb")
    assert await store.delete("kb", ids=["r1"]) == 1
    await store.drop_collection("kb")
    await store.close()

    assert client.rows == []
    assert client.dropped
    assert client.closed


async def test_cancellation_reaches_native_load_and_never_caches_unready_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(exists=False, block_load=True)
    store, _, _ = _store(monkeypatch, client)
    loading = asyncio.create_task(store.ensure_collection("kb", dim=4))
    try:
        await asyncio.wait_for(client.load_started.wait(), 1)
        loading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loading
        assert client.load_cancelled.is_set()
        assert "kb" not in store._dims  # noqa: SLF001
        assert client.exists and not client.dropped
    finally:
        client.allow_load.set()
        loading.cancel()
        await asyncio.gather(loading, return_exceptions=True)
