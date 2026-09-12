"""Milvus BM25 schema v2 与数据库隔离测试。"""

from __future__ import annotations

import asyncio
from threading import Event
from typing import Any

import pytest
from pymilvus import DataType, Function, FunctionType, MilvusClient
from pymilvus.client.types import LoadState

from comet_rag.infrastructure.persistence.vector_store import milvus as target
from comet_rag.ports import CollectionNotFound, CollectionSchemaMismatch, VectorRecord


def _v2_schema(*, dim: int = 4) -> dict[str, Any]:
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
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


class _FakeSync:
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
        self.load_started = Event()
        self.allow_load = Event()
        self.load_state = LoadState.Loaded if exists else LoadState.NotExist
        self.created: dict[str, Any] | None = None
        self.indexes: Any = None
        self.loaded: dict[str, Any] | None = None
        self.dropped = False

    def has_collection(self, name: str) -> bool:
        return self.exists

    def describe_collection(self, name: str) -> dict[str, Any]:
        return self.description

    def list_indexes(self, name: str, *, field_name: str) -> list[str]:
        self.list_index_calls += 1
        if self.list_index_calls <= self.index_ready_after:
            return []
        return ["sparse_vector"]

    def describe_index(self, name: str, index_name: str) -> dict[str, Any] | None:
        if self.describe_index_none:
            return None
        return _v2_index()

    def create_schema(self, **kwargs: Any):
        return MilvusClient.create_schema(**kwargs)

    def prepare_index_params(self):
        return MilvusClient.prepare_index_params()

    def create_collection(self, name: str, **kwargs: Any) -> None:
        if self.create_conflict:
            self.exists = True
            self.load_state = LoadState.Loaded
            raise RuntimeError("collection already exists")
        self.created = {"name": name, **kwargs}
        self.exists = True
        self.load_state = LoadState.NotLoad

    def create_index(self, name: str, *, index_params: Any) -> None:
        if self.fail_index:
            raise RuntimeError("index failed")
        self.indexes = index_params

    def load_collection(self, name: str, **kwargs: Any) -> None:
        self.load_calls += 1
        self.load_state = LoadState.Loading
        self.load_started.set()
        if self.block_load and not self.allow_load.wait(timeout=2):
            raise TimeoutError("test did not release load")
        if self.fail_load:
            raise RuntimeError("load failed")
        if self.delete_on_load:
            self.exists = False
            self.load_state = LoadState.NotExist
            return
        self.load_state = LoadState.Loaded
        self.loaded = {"name": name, **kwargs}

    def get_load_state(self, name: str) -> dict[str, LoadState]:
        return {"state": self.load_state}

    def drop_collection(self, name: str) -> None:
        self.dropped = True
        self.exists = False
        self.load_state = LoadState.NotExist


class _FakeAsync:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def upsert(self, name: str, rows: list[dict[str, Any]]) -> None:
        self.rows = rows


def _store(
    monkeypatch: pytest.MonkeyPatch, sync: _FakeSync
) -> tuple[target.MilvusStore, _FakeAsync, list[dict[str, Any]]]:
    async_client = _FakeAsync()
    constructor_args: list[dict[str, Any]] = []

    def sync_factory(**kwargs: Any) -> _FakeSync:
        constructor_args.append(kwargs)
        return sync

    def async_factory(**kwargs: Any) -> _FakeAsync:
        constructor_args.append(kwargs)
        return async_client

    monkeypatch.setattr(target, "MilvusClient", sync_factory)
    monkeypatch.setattr(target, "AsyncMilvusClient", async_factory)
    store = target.MilvusStore(
        endpoint="http://milvus.invalid:19530",
        database_name="zhihao_test_database",
        prefix="ct_schema",
    )
    return store, async_client, constructor_args


async def test_new_collection_uses_bm25_schema_and_server_generated_sparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=False)
    store, async_client, constructors = _store(monkeypatch, sync)

    await store.aensure_collection("kb", dim=4)
    await store.aupsert(
        "kb", [VectorRecord(id="r1", text="中文 BM25", embedding=[1, 0, 0, 0])]
    )

    assert all(args["db_name"] == "zhihao_test_database" for args in constructors)
    assert sync.created is not None
    schema = sync.created["schema"].to_dict()
    indexes = [item.to_dict() for item in sync.indexes]
    assert target._schema_mismatches(schema, indexes) == []  # noqa: SLF001
    assert sync.loaded is not None
    assert sync.loaded["replica_number"] == 1
    assert "sparse_vector" not in async_client.rows[0]


async def test_old_schema_is_rejected_without_create_or_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_schema = _v2_schema()
    old_schema["functions"] = []
    sync = _FakeSync(exists=True, description=old_schema)
    store, _, _ = _store(monkeypatch, sync)

    with pytest.raises(CollectionSchemaMismatch, match="显式删除"):
        await store.aensure_collection("existing-kb", dim=4)

    assert sync.created is None
    assert sync.dropped is False


async def test_ensure_waits_for_concurrent_creator_to_finish_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=True, index_ready_after=2)
    store, _, _ = _store(monkeypatch, sync)
    monkeypatch.setattr(target, "_SCHEMA_READY_INTERVAL_SECONDS", 0)

    await store.aensure_collection("shared-kb", dim=4)

    assert sync.list_index_calls == 3
    assert store._dims["shared-kb"] == 4  # noqa: SLF001


async def test_ensure_recovers_when_create_loses_cross_process_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=False, create_conflict=True)
    store, _, _ = _store(monkeypatch, sync)

    await store.aensure_collection("shared-kb", dim=4)

    assert store._dims["shared-kb"] == 4  # noqa: SLF001
    assert "shared-kb" not in store._created  # noqa: SLF001


async def test_two_instances_wait_until_collection_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=False, block_load=True)
    first, _, _ = _store(monkeypatch, sync)
    second, _, _ = _store(monkeypatch, sync)

    first_task = asyncio.create_task(first.aensure_collection("shared-kb", dim=4))
    assert await asyncio.to_thread(sync.load_started.wait, 1)
    second_task = asyncio.create_task(second.aensure_collection("shared-kb", dim=4))
    for _ in range(100):
        if sync.load_calls == 2:
            break
        await asyncio.sleep(0.01)

    assert sync.load_calls == 2
    assert not first_task.done()
    assert not second_task.done()

    sync.allow_load.set()
    await asyncio.gather(first_task, second_task)

    assert sync.load_state == LoadState.Loaded
    assert first._dims["shared-kb"] == 4  # noqa: SLF001
    assert second._dims["shared-kb"] == 4  # noqa: SLF001


async def test_load_failure_keeps_collection_and_does_not_cache_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=False, fail_load=True)
    store, _, _ = _store(monkeypatch, sync)

    with pytest.raises(RuntimeError, match="load failed"):
        await store.aensure_collection("retryable-kb", dim=4)

    assert sync.exists is True
    assert sync.dropped is False
    assert "retryable-kb" not in store._dims  # noqa: SLF001

    sync.fail_load = False
    await store.aensure_collection("retryable-kb", dim=4)
    assert store._dims["retryable-kb"] == 4  # noqa: SLF001


async def test_collection_deleted_during_load_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=False, delete_on_load=True)
    store, _, _ = _store(monkeypatch, sync)

    with pytest.raises(CollectionNotFound):
        await store.aensure_collection("deleted-kb", dim=4)

    assert "deleted-kb" not in store._dims  # noqa: SLF001


async def test_missing_index_description_becomes_stable_schema_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=True, describe_index_none=True)
    store, _, _ = _store(monkeypatch, sync)
    monkeypatch.setattr(target, "_SCHEMA_READY_ATTEMPTS", 1)

    with pytest.raises(CollectionSchemaMismatch, match="SPARSE_INVERTED_INDEX"):
        await store.aensure_collection("existing-kb", dim=4)


@pytest.mark.parametrize("mode", ["dense", "keyword"])
async def test_direct_search_after_restart_rejects_old_schema(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """进程重启后的空缓存不能让读路径绕过 schema v2 校验。"""
    old_schema = _v2_schema()
    old_schema["functions"] = []
    sync = _FakeSync(exists=True, description=old_schema)
    store, _, _ = _store(monkeypatch, sync)

    with pytest.raises(CollectionSchemaMismatch, match="BM25 function"):
        if mode == "dense":
            await store.asearch("existing-kb", [1.0, 0.0, 0.0, 0.0])
        else:
            await store.asearch_keywords("existing-kb", "量子")

    assert store._dims == {}  # noqa: SLF001
    assert sync.dropped is False


async def test_new_collection_is_rolled_back_when_index_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = _FakeSync(exists=False, fail_index=True)
    store, _, _ = _store(monkeypatch, sync)

    with pytest.raises(RuntimeError, match="index failed"):
        await store.aensure_collection("new-kb", dim=4)

    assert sync.dropped is True


def test_database_name_is_required_before_clients_are_created() -> None:
    with pytest.raises(ValueError, match="database_name"):
        target.MilvusStore(database_name="   ")
