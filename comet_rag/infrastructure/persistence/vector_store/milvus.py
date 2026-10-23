"""Milvus 向量存储适配器。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Any, cast

from pymilvus import (
    AsyncMilvusClient,
    DataType,
    Function,
    FunctionType,
)
from pymilvus.client.types import LoadState

from comet_rag.core.logging import logger
from comet_rag.ports import (
    AsyncVectorStore,
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
_BM25_INDEX_MISSING = f"{_SPARSE} 缺少 BM25 SPARSE_INVERTED_INDEX"

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
    return json.dumps(key, ensure_ascii=False)[1:-1]


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
    """兼容旧调用的字面量渲染；RPC 路径统一使用 compile_filter。

    保留现有导入方的返回类型，不作为不支持模板参数时的自动回退。
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


def compile_filter(filter: Filter | None) -> tuple[str, dict[str, Any]]:
    """编译现有等值/IN 契约；值绑定，JSON 路径仍按字面量转义。

    Any 来自现有 Filter 边界；这里拒绝 SDK 无法可靠表示的值，不猜测字符串含义。
    不支持的值不回退拼接，也不在 RPC 失败后去掉过滤条件重试。
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (key, expected) in enumerate((filter or {}).items()):
        field = f'{_METADATA}["{_quote_key(key)}"]'
        membership = isinstance(expected, (list, tuple, set))
        values = list(expected) if membership else [expected]
        for value in values:
            if not isinstance(value, (str, bool, int, float)):
                raise ValueError("Milvus 过滤值仅支持字符串、布尔和有限数值")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Milvus 过滤数值必须有限")
            if isinstance(value, int) and not -(2**63) <= value < 2**63:
                raise ValueError("Milvus 过滤整数必须在 int64 范围内")
        if membership and len({type(value) for value in values}) > 1:
            raise ValueError("Milvus IN 过滤值必须同类型")
        placeholder = f"f{index}"
        params[placeholder] = values if membership else expected
        operator = "in" if membership else "=="
        clauses.append(f"{field} {operator} {{{placeholder}}}")
    return " and ".join(clauses), params


def _combine(*expressions: str) -> str:
    parts = [e for e in expressions if e]
    return " and ".join(f"({p})" for p in parts) if parts else ""


def _schema_mismatches(
    description: dict[str, Any], indexes: Sequence[dict[str, Any]] | None
) -> list[str]:
    """列出 schema v2 差异；读索引前先用 None 校验字段，避免未知字段 RPC。"""
    fields = {
        str(field.get("name")): field
        for field in description.get("fields", [])
        if isinstance(field, dict)
    }
    required_types = {
        _ID: DataType.VARCHAR,
        _TEXT: DataType.VARCHAR,
        _METADATA: DataType.JSON,
        _DENSE: DataType.FLOAT_VECTOR,
        _SPARSE: DataType.SPARSE_FLOAT_VECTOR,
    }
    problems = []
    for name, expected in required_types.items():
        field = fields.get(name)
        if field is None:
            problems.append(f"缺少 {name} 字段")
        elif field.get("type") != expected:
            problems.append(f"{name} 类型不是 {expected.name}")
    if problems:
        return problems

    primary = fields[_ID]
    if primary.get("is_primary") not in (True, "true", "True"):
        problems.append(f"{_ID} 不是主键")
    # upsert 必须保留调用方提供的稳定 ID，服务端生成主键会破坏幂等写入。
    if any(
        auto_id in (True, "true", "True")
        for auto_id in (description.get("auto_id"), primary.get("auto_id"))
    ):
        problems.append(f"{_ID} 启用了 auto_id")

    text_field = fields[_TEXT]
    params = text_field.get("params") or {}
    analyzer_enabled = params.get("enable_analyzer")
    if analyzer_enabled not in (True, "true", "True"):
        problems.append(f"{_TEXT} 未启用 analyzer")

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

    if indexes is None:
        return problems

    def _index_value(index: dict[str, Any], key: str) -> Any:
        return index.get(key) or (index.get("params") or {}).get(key)

    has_bm25_index = any(
        index.get("field_name") == _SPARSE
        and _index_value(index, "index_type") == "SPARSE_INVERTED_INDEX"
        and _index_value(index, "metric_type") == "BM25"
        for index in indexes
    )
    if not has_bm25_index:
        problems.append(_BM25_INDEX_MISSING)
    return problems


class AsyncMilvusStore(AsyncVectorStore):
    def __init__(
        self,
        *,
        endpoint: str = "http://localhost:19530",
        database_name: str,
        api_key: str | None = None,
        prefix: str = "comet",
        #: revision 跨客户端切换须传 Strong；Session 只用于旧链路。
        consistency_level: str = "Session",
        metric_type: str = "COSINE",
        replica_number: int = 1,
    ) -> None:
        if not database_name.strip():
            raise ValueError("database_name 必须是非空字符串")
        if replica_number <= 0:
            raise ValueError("replica_number 必须是正整数")
        token = api_key or ""
        # SDK 惰性连接：配置在构造时校验，连通性错误在首次 awaited 操作暴露。
        self._client = AsyncMilvusClient(
            uri=endpoint, token=token, db_name=database_name
        )
        self._prefix = prefix
        self._consistency = consistency_level
        self._metric = metric_type
        self._replica_number = replica_number
        #: kb_id → 维度。避免每次写入都去 describe 一次。
        self._dims: dict[str, int] = {}
        #: 记录本实例确认创建的 collection，不代表它仍只被本实例使用。
        self._created: set[str] = set()

    # ── 集合 ───────────────────────────────────────────────────────────────

    @property
    def consistency_level(self) -> str:
        """组合根检查跨客户端 revision 可见性的启动前提。"""
        return self._consistency

    def _name(self, kb_id: str) -> str:
        return collection_name_for(kb_id, prefix=self._prefix)

    async def _describe(self, name: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._client.describe_collection(name),
        )

    async def _sparse_indexes(self, name: str) -> list[dict[str, Any]]:
        index_names = cast(
            "list[str]",
            await self._client.list_indexes(name, field_name=_SPARSE),
        )
        indexes: list[dict[str, Any]] = []
        for index_name in index_names:
            description = await self._client.describe_index(name, index_name)
            # 列表与详情间可能发生删除；未知详情不等于确定缺失，不能据此补建。
            indexes.append(
                description
                if isinstance(description, dict)
                else {"index_name": index_name}
            )
        return indexes

    @staticmethod
    def _dense_dim(description: dict[str, Any], kb_id: str) -> int:
        for field in description.get("fields", []):
            if field.get("name") == _DENSE:
                return int(field["params"]["dim"])
        raise CollectionSchemaMismatch(kb_id, [f"缺少 {_DENSE} 字段"])

    async def _dim_of(self, kb_id: str) -> int:
        """只缓存已通过 schema 校验的维度；联网检查由 SDK 原生异步执行。"""
        cached = self._dims.get(kb_id)
        if cached is not None:
            return cached
        name = self._name(kb_id)
        if not await self._client.has_collection(name):
            raise CollectionNotFound(kb_id)
        dim = await self._validated_dim(name, kb_id)
        self._dims[kb_id] = dim
        return dim

    async def _inspect_collection(
        self, name: str, kb_id: str
    ) -> tuple[int, list[dict[str, Any]], list[str]]:
        """读取维度、已有 sparse 索引和兼容性差异，不改变服务端状态。"""
        description = await self._describe(name)
        schema_problems = _schema_mismatches(description, None)
        if schema_problems:
            raise CollectionSchemaMismatch(kb_id, schema_problems)
        dim = self._dense_dim(description, kb_id)
        indexes = await self._sparse_indexes(name)
        problems = _schema_mismatches(description, indexes)
        return dim, indexes, problems

    async def _validated_dim(self, name: str, kb_id: str) -> int:
        """读写路径只校验；续建索引属于显式 ensure_collection 的职责。"""
        dim, _, problems = await self._inspect_collection(name, kb_id)
        if problems:
            raise CollectionSchemaMismatch(kb_id, problems)
        return dim

    async def _ensure_existing_ready(self, name: str, kb_id: str, dim: int) -> None:
        """仅续建兼容 schema 中确实缺失的索引，绝不替换已有索引。"""
        self._dims.pop(kb_id, None)
        existing, indexes, problems = await self._inspect_collection(name, kb_id)
        if existing != dim:
            raise DimensionMismatch(kb_id, existing, dim)
        if problems == [_BM25_INDEX_MISSING] and not indexes:
            # 创建者可能已取消或退出。重复相同参数的建索引由 Milvus 幂等处理，
            # 不能只等一个可能再也不会继续的创建者，也不能删除它留下的 collection。
            await self._create_indexes(name)
            await self._validated_dim(name, kb_id)
        elif problems:
            raise CollectionSchemaMismatch(kb_id, problems)
        await self._ensure_loaded(name, kb_id)
        self._dims[kb_id] = existing

    async def _create_indexes(self, name: str) -> None:
        index = self._client.prepare_index_params()
        index.add_index(
            field_name=_DENSE, index_type="AUTOINDEX", metric_type=self._metric
        )
        index.add_index(
            field_name=_SPARSE,
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        await self._client.create_index(name, index_params=index)

    async def _ensure_loaded(self, name: str, kb_id: str) -> None:
        """主动加载 collection，并在服务端确认完成后返回。"""
        self._dims.pop(kb_id, None)
        result = cast(
            "dict[str, Any]",
            await self._client.get_load_state(name),
        )
        state = result.get("state")
        if state == LoadState.NotExist:
            raise CollectionNotFound(kb_id)
        if state != LoadState.Loaded:
            # SDK 的 load_collection 会等待服务端完成；对 NotLoad 与 Loading
            # 都主动调用，避免依赖另一个可能已经退出的创建进程。
            await self._client.load_collection(
                name,
                replica_number=self._replica_number,
            )
            result = cast(
                "dict[str, Any]",
                await self._client.get_load_state(name),
            )
            state = result.get("state")
        if state == LoadState.Loaded:
            return
        if state == LoadState.NotExist:
            raise CollectionNotFound(kb_id)
        raise RuntimeError(
            f"Milvus collection 加载调用结束但状态不是 Loaded kb={kb_id} state={state}"
        )

    async def ensure_collection(self, kb_id: str, *, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"维度必须为正整数，收到 {dim}")
        name = self._name(kb_id)

        if await self._client.has_collection(name):
            await self._ensure_existing_ready(name, kb_id, dim)
            return

        # Schema 是纯本地构造，不执行网络请求。
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
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

        try:
            await self._client.create_collection(
                name,
                schema=schema,
                consistency_level=self._consistency,
            )
        except Exception:
            # 跨进程没有共享锁。若别的实例刚好抢先建表，本次 create 会失败；
            # 重新探测并续建缺失索引即可。其余错误保留原异常。
            if not await self._client.has_collection(name):
                raise
            await self._ensure_existing_ready(name, kb_id, dim)
            return
        self._created.add(kb_id)
        try:
            await self._create_indexes(name)
            await self._ensure_loaded(name, kb_id)
        except Exception:
            # RPC 失败不代表服务端未提交；另一实例可能已经写入。取消同样保留
            # collection，下一次 ensure 可续建索引或加载，而不猜测数据所有权。
            logger.exception(
                f"Milvus collection 初始化失败，保留供重试 kb={kb_id} name={name}"
            )
            raise
        self._dims[kb_id] = dim
        logger.info(f"Milvus collection 已创建 kb={kb_id} name={name} dim={dim}")

    async def drop_collection(self, kb_id: str) -> None:
        self._dims.pop(kb_id, None)
        name = self._name(kb_id)
        if await self._client.has_collection(name):
            await self._client.drop_collection(name)
        self._created.discard(kb_id)

    # ── 写入 ───────────────────────────────────────────────────────────────

    async def upsert(self, kb_id: str, records: Sequence[VectorRecord]) -> list[str]:
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
        await self._client.upsert(self._name(kb_id), rows)
        return [record.id for record in records]

    async def delete(
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

        expression, params = compile_filter(filter)
        id_expr = ""
        if ids is not None:
            if not ids:
                return 0
            params["ids"] = list(ids)
            id_expr = f"{_ID} in {{ids}}"
        expression = _combine(id_expr, expression)

        # Milvus 的 delete 不返回准确条数，先查出真正会被删的主键。
        # 直接信它的 delete_count 会让契约里"删除不存在的 id 返回 0"失效。
        matched = await self._client.query(
            name, filter=expression, filter_params=params, output_fields=[_ID]
        )
        keys = [row[_ID] for row in matched]
        if not keys:
            return 0
        await self._client.delete(name, ids=keys)
        return len(keys)

    # ── 读取 ───────────────────────────────────────────────────────────────

    async def search(
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

        expression, params = compile_filter(filter)
        results = await self._client.search(
            self._name(kb_id),
            data=[list(query_embedding)],
            anns_field=_DENSE,
            limit=top_k,
            filter=expression,
            filter_params=params,
            output_fields=[_TEXT, _METADATA],
            consistency_level=self._consistency,
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

    async def search_keywords(
        self,
        kb_id: str,
        query: str,
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0:
            raise ValueError(f"top_k 必须为正整数，收到 {top_k}")
        await self._dim_of(kb_id)
        if not query.strip():
            return []

        expression, params = compile_filter(filter)
        results = await self._client.search(
            self._name(kb_id),
            data=[query],
            anns_field=_SPARSE,
            search_params={"metric_type": "BM25"},
            limit=top_k,
            filter=expression,
            filter_params=params,
            output_fields=[_TEXT, _METADATA],
            consistency_level=self._consistency,
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
        hits.sort(key=lambda hit: (-hit.score, hit.id))
        return hits

    async def count(self, kb_id: str, *, filter: Filter | None = None) -> int:
        await self._dim_of(kb_id)
        expression, params = compile_filter(filter)
        rows = await self._client.query(
            self._name(kb_id),
            filter=expression,
            filter_params=params,
            output_fields=["count(*)"],
            consistency_level=self._consistency,
        )
        return int(rows[0]["count(*)"]) if rows else 0

    async def close(self) -> None:
        await self._client.close()


__all__ = [
    "AsyncMilvusStore",
    "build_expression",
    "compile_filter",
    "collection_name_for",
]
