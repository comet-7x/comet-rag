from __future__ import annotations

import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pymilvus import AsyncMilvusClient
from pymilvus.client.prepare import Prepare

from comet_rag.infrastructure.persistence.vector_store.milvus import (
    AsyncMilvusStore,
    compile_filter,
)
from comet_rag.ports import Filter


@pytest.mark.parametrize(
    "value", ['"] or true or metadata["x', '中文\n\t\\"{f0}', "", True, 42, 1.5]
)
def test_values_are_bound_and_encoded_by_real_sdk(value: object) -> None:
    expression, params = compile_filter({"tag": value})
    assert expression == 'metadata["tag"] == {f0}'
    assert params == {"f0": value}
    encoded = Prepare.prepare_expression_template(params)
    assert encoded["f0"].WhichOneof("val") is not None


@pytest.mark.parametrize(
    "key", ['"] or true or metadata["x', "a\\b", "a\n\t\x00b", "中文"]
)
def test_keys_remain_one_json_path_literal(key: str) -> None:
    expression, params = compile_filter({key: "value"})
    literal = expression.removeprefix("metadata[").removesuffix("] == {f0}")
    assert json.loads(literal) == key
    assert params == {"f0": "value"}


@pytest.mark.parametrize("values", [["北京", "上海"], ("a", "b"), {"a", "b"}, []])
def test_membership_uses_one_independent_bound_array(values) -> None:
    expression, params = compile_filter({"city": values})
    assert expression == 'metadata["city"] in {f0}'
    assert sorted(params["f0"]) == sorted(values)
    Prepare.prepare_expression_template(params)
    if isinstance(values, list):
        values.append("later")
        assert "later" not in params["f0"]


@pytest.mark.parametrize(
    "value",
    [None, {}, object(), float("nan"), float("inf"), 2**63, ["a", 1], [True, 1]],
)
def test_unsupported_filter_values_fail_explicitly(value: object) -> None:
    with pytest.raises(ValueError):
        compile_filter({"tag": value})


class Client:
    def __init__(self) -> None:
        self.search = AsyncMock(return_value=[[]])
        self.query = AsyncMock(return_value=[{"id": "selected", "count(*)": 1}])
        self.delete = AsyncMock()


def store() -> tuple[AsyncMilvusStore, Client]:
    client = Client()
    result = AsyncMilvusStore.__new__(AsyncMilvusStore)
    result._client = client  # type: ignore[assignment] # 无网络的 SDK 边界替身。
    result._dims = {"kb": 2}
    result._prefix = "binding"
    result._consistency = "Strong"
    return result, client


async def invoke(
    target: AsyncMilvusStore, operation: str, value: object = "tenant"
) -> None:
    # 故障注入故意跨过静态契约，验证存储入口仍会拒绝非法运行时值。
    condition = cast("Filter", {"tenant": value})
    if operation == "dense":
        await target.search("kb", [1.0, 0.0], filter=condition)
    elif operation == "keyword":
        await target.search_keywords("kb", "query", filter=condition)
    elif operation == "count":
        await target.count("kb", filter=condition)
    else:
        await target.delete("kb", ids=['id"] or true'], filter=condition)


@pytest.mark.parametrize("operation", ["count", "delete"])
async def test_integrity_reads_are_strong_even_when_retrieval_uses_eventual(
    operation: str,
) -> None:
    target, client = store()
    target._consistency = "Eventually"
    await invoke(target, operation)
    assert client.query.call_args.kwargs["consistency_level"] == "Strong"


@pytest.mark.parametrize("operation", ["dense", "keyword", "count", "delete"])
async def test_every_read_and_delete_selection_preserves_binding(
    operation: str,
) -> None:
    target, client = store()
    await invoke(target, operation)
    call = (
        client.search.call_args
        if operation in ("dense", "keyword")
        else client.query.call_args
    )
    assert call.kwargs["filter_params"]["f0"] == "tenant"
    assert 'metadata["tenant"] == {f0}' in call.kwargs["filter"]
    if operation == "delete":
        assert call.kwargs["filter"] == '(id in {ids}) and (metadata["tenant"] == {f0})'
        assert call.kwargs["filter_params"]["ids"] == ['id"] or true']
        client.delete.assert_awaited_once_with(target._name("kb"), ids=["selected"])


@pytest.mark.parametrize("operation", ["dense", "keyword", "count", "delete"])
@pytest.mark.parametrize("failure_type", [TimeoutError, asyncio.CancelledError])
async def test_rpc_failure_never_retries_without_filter(
    operation: str, failure_type: type[BaseException]
) -> None:
    target, client = store()
    failure = failure_type("injected")
    rpc = client.search if operation in ("dense", "keyword") else client.query
    rpc.side_effect = failure
    with pytest.raises(failure_type) as error:
        await invoke(target, operation)
    assert error.value is failure
    assert rpc.await_count == 1
    client.delete.assert_not_awaited()


@pytest.mark.parametrize("operation", ["dense", "keyword", "count", "delete"])
async def test_invalid_filter_does_not_reach_query_or_delete(operation: str) -> None:
    target, client = store()
    with pytest.raises(ValueError):
        await invoke(target, operation, float("nan"))
    client.search.assert_not_awaited()
    client.query.assert_not_awaited()
    client.delete.assert_not_awaited()


@pytest.mark.parametrize("operation", ["search", "query"])
async def test_native_sdk_forwards_bound_values_to_transport(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    client = AsyncMilvusClient.__new__(AsyncMilvusClient)
    transport = Client()
    monkeypatch.setattr(client, "_get_connection", AsyncMock(return_value=transport))
    monkeypatch.setattr(client, "_generate_call_context", lambda **kwargs: None)
    expression, params = compile_filter({"tag": '中文\\"] or true'})
    if operation == "search":
        await client.search(
            "binding", data=[[1.0, 0.0]], filter=expression, filter_params=params
        )
        call = transport.search.call_args
    else:
        await client.query("binding", filter=expression, filter_params=params)
        call = transport.query.call_args
    assert call.kwargs["expr_params"] == params
    expression_key = "expression" if operation == "search" else "expr"
    assert call.kwargs[expression_key] == expression


def test_multiple_fields_have_distinct_placeholders() -> None:
    expression, params = compile_filter({"tenant": "a", "revision": ["r1", "r2"]})
    assert expression == 'metadata["tenant"] == {f0} and metadata["revision"] in {f1}'
    assert params == {"f0": "a", "f1": ["r1", "r2"]}
