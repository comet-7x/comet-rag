"""`BaseVectorStore` 的 Milvus 实现。

需要 `milvus` extra（`pip install comet-rag[milvus]`）。

## 三个关键决定，都是实测出来的而非猜的

**一致性级别用 `Session`，不是每次写完 flush。**
Milvus 默认的 `Bounded` 一致性下，写入后立刻检索**命中 0 条**（实测）。
契约要求"`aupsert` 返回即可查"（plan R1），有两条路能满足：
  · 每次写完 `flush()` —— 会封存 segment，吞吐直接崩，是错的解法
  · `Session` 一致性 —— "读己所写"，同一 client 立刻看得到
实测三种级别的往返耗时：Bounded 0.22s（但查不到）、Session 0.34s、
Strong 0.62s。Session 恰好给出契约要求的语义，代价只有 Strong 的一半。

**一个知识库一个 collection**，而非共用 collection 靠 partition key 分。
隔离更彻底，且换 embedding 模型时可以单库重建而不影响其他库。
代价是 Milvus 单实例的 collection 数量有上限（几百量级）——
真撞上那天会有真实负载数据来指导迁移，比现在拍脑袋强。

**预留 sparse 向量字段**（spec A11），M1 不写入内容。
Milvus 要求所有向量字段在 load 前都有索引，所以字段和索引一起建；
写入时该字段给空 dict `{}`（实测可接受，省略字段则报错）。
这样 M3 做混合检索时是纯加代码，不必重灌数据。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from typing import Any, cast

from pymilvus import AsyncMilvusClient, DataType, MilvusClient

from comet_rag.core.logging import logger
from comet_rag.infrastructure.vectorstore.base import (
    BaseVectorStore,
    CollectionNotFound,
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

    **键和值一样需要转义**（PR 评审 #5）。原来只转义了值，键是直接插进
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


class MilvusStore(BaseVectorStore):
    def __init__(
        self,
        *,
        endpoint: str = "http://localhost:19530",
        api_key: str | None = None,
        prefix: str = "comet",
        #: "读己所写"。改成 Bounded 会让"写完立即查"失效 —— 那正是 plan R1。
        consistency_level: str = "Session",
        metric_type: str = "COSINE",
    ) -> None:
        token = api_key or ""
        self._sync = MilvusClient(uri=endpoint, token=token)
        self._async = AsyncMilvusClient(uri=endpoint, token=token)
        self._prefix = prefix
        self._consistency = consistency_level
        self._metric = metric_type
        #: kb_id → 维度。避免每次写入都去 describe 一次。
        self._dims: dict[str, int] = {}

    # ── 集合 ───────────────────────────────────────────────────────────────

    def _name(self, kb_id: str) -> str:
        return collection_name_for(kb_id, prefix=self._prefix)

    async def _dim_of(self, kb_id: str) -> int:
        """读回已有 collection 的向量维度。不存在则抛 `CollectionNotFound`。

        同步客户端的调用一律甩进线程（PR 评审 #8）：`has_collection` /
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
        # cast 的原因不在我们这边：pymilvus 的 `describe_collection` 没有返回
        # 标注，pyright 顺着它的内部实现推成了协程类型。运行时它是同步的
        # （否则这里早就炸了），所以在边界上把类型钉住。
        desc = cast(
            "dict[str, Any]",
            await asyncio.to_thread(self._sync.describe_collection, name),
        )
        for field in desc["fields"]:
            if field["name"] == _DENSE:
                dim = int(field["params"]["dim"])
                self._dims[kb_id] = dim
                return dim
        raise CollectionNotFound(kb_id)

    async def aensure_collection(self, kb_id: str, *, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"维度必须为正整数，收到 {dim}")
        name = self._name(kb_id)

        if await asyncio.to_thread(self._sync.has_collection, name):
            existing = await self._dim_of(kb_id)
            if existing != dim:
                raise DimensionMismatch(kb_id, existing, dim)
            return

        # 这两个是纯本地构造（不打网络），留在事件循环上没问题
        schema = self._sync.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(_ID, DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field(_TEXT, DataType.VARCHAR, max_length=65535)
        schema.add_field(_METADATA, DataType.JSON)
        schema.add_field(_DENSE, DataType.FLOAT_VECTOR, dim=dim)
        # A11：预留给 M3 的混合检索。字段必须建表时声明 —— 事后加要重灌数据。
        schema.add_field(_SPARSE, DataType.SPARSE_FLOAT_VECTOR)

        index = self._sync.prepare_index_params()
        index.add_index(
            field_name=_DENSE, index_type="AUTOINDEX", metric_type=self._metric
        )
        # Milvus 要求所有向量字段在 load 前都有索引，预留字段也不例外
        index.add_index(
            field_name=_SPARSE, index_type="SPARSE_INVERTED_INDEX", metric_type="IP"
        )

        await asyncio.to_thread(
            self._sync.create_collection,
            name,
            schema=schema,
            index_params=index,
            consistency_level=self._consistency,
        )
        self._dims[kb_id] = dim
        logger.info(f"Milvus collection 已创建 kb={kb_id} name={name} dim={dim}")

    async def adrop_collection(self, kb_id: str) -> None:
        self._dims.pop(kb_id, None)
        name = self._name(kb_id)
        if await asyncio.to_thread(self._sync.has_collection, name):
            await asyncio.to_thread(self._sync.drop_collection, name)

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
                # 省略该字段会报 DataNotMatch；空 dict 是被接受的（实测）
                _SPARSE: {},
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


__all__ = ["MilvusStore", "build_expression", "collection_name_for"]
