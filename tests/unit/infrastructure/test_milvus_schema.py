"""Milvus BM25 schema v2 与数据库隔离测试。"""

from __future__ import annotations

from typing import Any

import pytest
from pymilvus import DataType, Function, FunctionType, MilvusClient

from comet_rag.infrastructure.vectorstore import milvus as target
from comet_rag.ports import CollectionSchemaMismatch, VectorRecord


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
    ):
        self.exists = exists
        self.description = description or _v2_schema()
        self.fail_index = fail_index
        self.created: dict[str, Any] | None = None
        self.indexes: Any = None
        self.loaded: dict[str, Any] | None = None
        self.dropped = False

    def has_collection(self, name: str) -> bool:
        return self.exists

    def describe_collection(self, name: str) -> dict[str, Any]:
        return self.description

    def list_indexes(self, name: str, *, field_name: str) -> list[str]:
        return ["sparse_vector"]

    def describe_index(self, name: str, index_name: str) -> dict[str, Any]:
        return _v2_index()

    def create_schema(self, **kwargs: Any):
        return MilvusClient.create_schema(**kwargs)

    def prepare_index_params(self):
        return MilvusClient.prepare_index_params()

    def create_collection(self, name: str, **kwargs: Any) -> None:
        self.created = {"name": name, **kwargs}

    def create_index(self, name: str, *, index_params: Any) -> None:
        if self.fail_index:
            raise RuntimeError("index failed")
        self.indexes = index_params

    def load_collection(self, name: str, **kwargs: Any) -> None:
        self.loaded = {"name": name, **kwargs}

    def drop_collection(self, name: str) -> None:
        self.dropped = True


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
