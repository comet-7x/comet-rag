"""Milvus 向量存储适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, cast

from pymilvus import (
    AsyncMilvusClient,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
)

from comet_rag.core.logging import logger
from comet_rag.ports import (
    BaseVectorStore,
    CollectionNotFound,
    CollectionSchemaMismatch,
    DimensionMismatch,
    Filter,
    SearchHit,
    VectorRecord,
)

_ID = "id"
_TEXT = "text"
_METADATA = "metadata"
_DENSE = "dense_vector"
_SPARSE = "sparse_vector"
_BM25_FUNCTION = "text_bm25"

_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def collection_name_for(kb_id: str, *, prefix: str = "comet") -> str:
    """kb_id → 合法的 Milvus collection 名。

    Milvus 只接受 `[A-Za-z_][A-Za-z0-9_]*`，而 kb_id 可以是任意字符串
    （中文、连字符都合法）。直接用会在建库时才炸，且报错很难懂。

    保留可读部分 + 追加 kb_id 的短哈希：既能在 Milvus 控制台里认出是哪个库，
    又保证不同 kb_id 不会映射到同一个名字（仅靠清洗的话 `a-b` 与 `a_b` 会撞）。
    """
    digest = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()[:12]
    readable = _UNSAFE.sub("_", kb_id)[:32].strip("_")
    return f"{prefix}_{readable}_{digest}" if readable else f"{prefix}_{digest}"


def _quote_key(key: str) -> str:
    """把元数据键渲染成 Milvus JSON 路径里的字面量。

    键和值都必须转义。键会被直接插进
    `metadata["..."]` 的 —— 一个带引号或反斜杠的键就能改变表达式结构，
    轻则查询报错，重则改变谓词语义。检索的 filter 来自 HTTP 请求体，
    也就是说这个键是调用方完全可控的。
    """
    if not isinstance(key, str) or not key:
        raise ValueError(f"元数据键必须是非空字符串，收到 {key!r}")
    return key.replace("\\", "\\\\").replace('"', '\\"')


def _quote(value: Any) -> str:
    """把值渲染成 Milvus 表达式字面量。

    字符串必须转义 —— 否则元数据里一个引号就能改变表达式语义
    （查询版的注入）。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_expression(filter: Filter | None) -> str:
    """结构化 dict → Milvus 布尔表达式。

    **翻译只发生在这里**，绝不让表达式语法泄漏到接口上（spec §7 Never）：
    调用方写 `{"kb_id": "x"}`，换 Qdrant 时改的是这一个函数，
    而不是每一个调用点。
    """
    if not filter:
        return ""
    clauses: list[str] = []
    for key, expected in filter.items():
        field = f'{_METADATA}["{_quote_key(key)}"]'
        if isinstance(expected, (list, tuple, set)):
            rendered = ", ".join(_quote(v) for v in expected)
            clauses.append(f"{field} in [{rendered}]")
        else:
            clauses.append(f"{field} == {_quote(expected)}")
    return " and ".join(clauses)


def _combine(*expressions: str) -> str:
    parts = [e for e in expressions if e]
    return " and ".join(f"({p})" for p in parts) if parts else ""


def _schema_mismatches(
    description: dict[str, Any], indexes: Sequence[dict[str, Any]]
) -> list[str]:
    """列出已有 collection 与 BM25 schema v2 的差异。"""
    fields = {
        str(field.get("name")): field
        for field in description.get("fields", [])
        if isinstance(field, dict)
    }
    text_field = fields.get(_TEXT)
    if text_field is None:
        return [f"缺少 {_TEXT} 字段"]

    params = text_field.get("params") or {}
    analyzer_enabled = params.get("enable_analyzer")
    if analyzer_enabled not in (True, "true", "True"):
        problems = [f"{_TEXT} 未启用 analyzer"]
    else:
        problems = []

    analyzer = params.get("analyzer_params")
    if isinstance(analyzer, str):
        try:
            analyzer = json.loads(analyzer)
        except json.JSONDecodeError:
            analyzer = None
    if not isinstance(analyzer, dict) or analyzer.get("type") != "chinese":
        problems.append(f"{_TEXT} analyzer 不是 chinese")

    functions = description.get("functions") or []
    has_bm25 = any(
        isinstance(function, dict)
        and function.get("name") == _BM25_FUNCTION
        and function.get("type") in (FunctionType.BM25, FunctionType.BM25.value, "BM25")
        and function.get("input_field_names") == [_TEXT]
        and function.get("output_field_names") == [_SPARSE]
        for function in functions
    )
    if not has_bm25:
        problems.append(f"缺少 {_TEXT} -> {_SPARSE} 的 BM25 function")

    def _index_value(index: dict[str, Any], key: str) -> Any:
        return index.get(key) or (index.get("params") or {}).get(key)

    has_bm25_index = any(
        index.get("field_name") == _SPARSE
        and _index_value(index, "index_type") == "SPARSE_INVERTED_INDEX"
        and _index_value(index, "metric_type") == "BM25"
        for index in indexes
    )
    if not has_bm25_index:
        problems.append(f"{_SPARSE} 缺少 BM25 SPARSE_INVERTED_INDEX")
    return problems


class MilvusStore(BaseVectorStore):
    def __init__(
        self,
        *,
        endpoint: str = "http://localhost:19530",
        database_name: str,
        api_key: str | None = None,
        prefix: str = "comet",
        #: "读己所写"。改成 Bounded 会让"写完立即查"失效 —— 那正是 plan R1。
        consistency_level: str = "Session",
        metric_type: str = "COSINE",
        replica_number: int = 1,
    ) -> None:
        if not database_name.strip():
            raise ValueError("database_name 必须是非空字符串")
        if replica_number <= 0:
            raise ValueError("replica_number 必须是正整数")
        token = api_key or ""
        self._sync = MilvusClient(uri=endpoint, token=token, db_name=database_name)
        self._async = AsyncMilvusClient(
            uri=endpoint, token=token, db_name=database_name
        )
        self._database_name = database_name
        self._prefix = prefix
        self._consistency = consistency_level
        self._metric = metric_type
        self._replica_number = replica_number
        #: kb_id → 维度。避免每次写入都去 describe 一次。
        self._dims: dict[str, int] = {}
        #: 仅记录本实例成功创建的 collection，供失败后的精确回收使用。
        self._created: set[str] = set()

    # ── 集合 ───────────────────────────────────────────────────────────────

    def _name(self, kb_id: str) -> str:
        return collection_name_for(kb_id, prefix=self._prefix)

    async def _describe(self, name: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await asyncio.to_thread(self._sync.describe_collection, name),
        )

    async def _sparse_indexes(self, name: str) -> list[dict[str, Any]]:
        index_names = cast(
            "list[str]",
            await asyncio.to_thread(
                self._sync.list_indexes, name, field_name=_SPARSE
            ),
        )
        return [
            cast(
                "dict[str, Any]",
                await asyncio.to_thread(self._sync.describe_index, name, index_name),
            )
            for index_name in index_names
        ]

    @staticmethod
    def _dense_dim(description: dict[str, Any], kb_id: str) -> int:
        for field in description.get("fields", []):
            if field.get("name") == _DENSE:
                return int(field["params"]["dim"])
        raise CollectionSchemaMismatch(kb_id, [f"缺少 {_DENSE} 字段"])

    async def _dim_of(self, kb_id: str) -> int:
        """读回已有 collection 的向量维度。不存在则抛 `CollectionNotFound`。

        同步客户端的调用一律放在线程中：`has_collection` /
        `describe_collection` 都是真网络请求，直接在事件循环上调，Milvus 一慢
        就会把整个进程堵住 —— API 的其他请求、runner 的取消检查点、worker 的
        心跳全部跟着停摆。**心跳停摆会被租约回收误判成 worker 已死**，
        于是一次 Milvus 抖动被放大成任务被重复执行。
        """
        cached = self._dims.get(kb_id)
        if cached is not None:
            return cached
        name = self._name(kb_id)
        if not await asyncio.to_thread(self._sync.has_collection, name):
            raise CollectionNotFound(kb_id)
        desc = await self._describe(name)
        dim = self._dense_dim(desc, kb_id)
        self._dims[kb_id] = dim
        return dim

    async def aensure_collection(self, kb_id: str, *, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"维度必须为正整数，收到 {dim}")
        name = self._name(kb_id)

        if await asyncio.to_thread(self._sync.has_collection, name):
            description = await self._describe(name)
            existing = self._dense_dim(description, kb_id)
            if existing != dim:
                raise DimensionMismatch(kb_id, existing, dim)
            problems = _schema_mismatches(
                description, await self._sparse_indexes(name)
            )
            if problems:
                raise CollectionSchemaMismatch(kb_id, problems)
            self._dims[kb_id] = existing
            return

        # 这两个是纯本地构造（不打网络），留在事件循环上没问题
        schema = self._sync.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(_ID, DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field(
            _TEXT,
            DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        )
        schema.add_field(_METADATA, DataType.JSON)
        schema.add_field(_DENSE, DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field(_SPARSE, DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name=_BM25_FUNCTION,
                function_type=FunctionType.BM25,
                input_field_names=[_TEXT],
                output_field_names=[_SPARSE],
            )
        )

        index = self._sync.prepare_index_params()
        index.add_index(
            field_name=_DENSE, index_type="AUTOINDEX", metric_type=self._metric
        )
        index.add_index(
            field_name=_SPARSE,
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )

        await asyncio.to_thread(
            self._sync.create_collection,
            name,
            schema=schema,
            consistency_level=self._consistency,
        )
        self._created.add(kb_id)
        try:
            await asyncio.to_thread(self._sync.create_index, name, index_params=index)
            await asyncio.to_thread(
                self._sync.load_collection,
                name,
                replica_number=self._replica_number,
            )
        except Exception:
            # 只回滚本调用刚创建的空 collection；已有 collection 永远不会走这里。
            try:
                await asyncio.to_thread(self._sync.drop_collection, name)
            except Exception:
                logger.exception(f"Milvus 新建失败且回滚失败 kb={kb_id} name={name}")
            else:
                self._created.discard(kb_id)
            raise
        self._dims[kb_id] = dim
        logger.info(f"Milvus collection 已创建 kb={kb_id} name={name} dim={dim}")

    async def adrop_collection(self, kb_id: str) -> None:
        self._dims.pop(kb_id, None)
        name = self._name(kb_id)
        if await asyncio.to_thread(self._sync.has_collection, name):
            await asyncio.to_thread(self._sync.drop_collection, name)
        self._created.discard(kb_id)

    # ── 写入 ───────────────────────────────────────────────────────────────

    async def aupsert(self, kb_id: str, records: Sequence[VectorRecord]) -> list[str]:
        dim = await self._dim_of(kb_id)
        # 先整体校验：宁可一条不写，也不要写一半留下不一致的库
        for record in records:
            if len(record.embedding) != dim:
                raise DimensionMismatch(kb_id, dim, len(record.embedding))
        if not records:
            return []

        rows = [
            {
                _ID: record.id,
                _TEXT: record.text,
                _METADATA: dict(record.metadata),
                _DENSE: list(record.embedding),
            }
            for record in records
        ]
        await self._async.upsert(self._name(kb_id), rows)
        return [record.id for record in records]

    async def adelete(
        self,
        kb_id: str,
        *,
        ids: Sequence[str] | None = None,
        filter: Filter | None = None,
    ) -> int:
        if ids is None and filter is None:
            raise ValueError("ids 与 filter 至少给一个，否则等于清空整个知识库")
        await self._dim_of(kb_id)  # 顺带校验 collection 存在
        name = self._name(kb_id)

        id_expr = ""
        if ids is not None:
            if not ids:
                return 0
            rendered = ", ".join(_quote(i) for i in ids)
            id_expr = f"{_ID} in [{rendered}]"
        expression = _combine(id_expr, build_expression(filter))

        # Milvus 的 delete 不返回准确条数，先查出真正会被删的主键。
        # 直接信它的 delete_count 会让契约里"删除不存在的 id 返回 0"失效。
        matched = await self._async.query(name, filter=expression, output_fields=[_ID])
        keys = [row[_ID] for row in matched]
        if not keys:
            return 0
        await self._async.delete(name, ids=keys)
        return len(keys)

    # ── 读取 ───────────────────────────────────────────────────────────────

    async def asearch(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        dim = await self._dim_of(kb_id)
        if len(query_embedding) != dim:
            raise DimensionMismatch(kb_id, dim, len(query_embedding))

        results = await self._async.search(
            self._name(kb_id),
            data=[list(query_embedding)],
            anns_field=_DENSE,
            limit=top_k,
            filter=build_expression(filter),
            output_fields=[_TEXT, _METADATA],
        )
        hits = [
            SearchHit(
                id=row["id"],
                text=row["entity"].get(_TEXT, ""),
                score=float(row["distance"]),
                metadata=row["entity"].get(_METADATA) or {},
            )
            for row in (results[0] if results else [])
        ]
        # id 作次级键，保证同分时顺序稳定（与内存实现一致）
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    async def acount(self, kb_id: str, *, filter: Filter | None = None) -> int:
        await self._dim_of(kb_id)
        rows = await self._async.query(
            self._name(kb_id),
            filter=build_expression(filter),
            output_fields=["count(*)"],
        )
        return int(rows[0]["count(*)"]) if rows else 0

    async def aclose(self) -> None:
        await self._async.close()
        await asyncio.to_thread(self._sync.close)


__all__ = [
    "MilvusStore",
    "build_expression",
    "collection_name_for",
]
