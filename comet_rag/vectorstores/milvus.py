from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pymilvus import (
    AsyncMilvusClient,
    DataType,
    Function,
    FunctionType,
    MilvusException,
)
from pymilvus.client.types import LoadState

from comet_rag.embedder.types import EmbeddingResult, EmbeddingSpace
from comet_rag.identity import canonical_json, stable_id
from comet_rag.vectorstores.base import (
    _binding,
    _ids,
    _limit,
    _query,
    _snapshot_records,
)
from comet_rag.vectorstores.filters import (
    Filter,
    _field_types,
    expression,
    validate_filter,
)
from comet_rag.vectorstores.types import (
    RecordVerification,
    VectorConfirmation,
    VectorHit,
    VectorRecord,
)

_IDENTITY = (
    "record_id",
    "representation_id",
    "unit_id",
    "source_id",
    "document_id",
    "index_scope",
    "index_profile_id",
    "space_id",
    "fingerprint",
)
_ANALYZER = {"type": "standard"}
_FUNCTION = "keyword_bm25"
_INDEXES = {
    "dense": ("dense_index", "FLAT"),
    "sparse": ("keyword_index", "SPARSE_INVERTED_INDEX"),
}
_MAX_FLOAT32 = (2 - 2**-23) * 2**127


def collection_name_for(index_scope: str) -> str:
    if not isinstance(index_scope, str):
        raise ValueError("index_scope must be a string")
    if not index_scope.strip():
        raise ValueError("index_scope must be nonblank")
    return "v2_scope_" + hashlib.sha256(index_scope.encode("utf-8")).hexdigest()


def _true(value: object) -> bool:
    return value is True or value in ("true", "True")


def _params(value: object) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, Mapping) else {}


class AsyncMilvusVectorStore:
    """每个 scope 使用稳定集合；注入的异步客户端由创建者关闭，单写者由调用方保证。"""

    def __init__(
        self,
        *,
        client: AsyncMilvusClient,
        index_scope: str,
        index_profile_id: str,
        space: EmbeddingSpace,
        field_types: Mapping[str, type] | None = None,
        timeout: float = 30.0,
    ) -> None:
        _binding(index_scope, index_profile_id, space)
        if isinstance(timeout, bool):
            raise ValueError("timeout must be positive and finite")
        if not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be positive and finite")
        if not math.isfinite(timeout):
            raise ValueError("timeout must be positive and finite")
        if timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self._client = client
        self._index_scope = index_scope
        self._index_profile_id = index_profile_id
        self._space = space
        self._fields = _field_types(field_types)
        self._timeout = timeout
        self._prepared = False
        self._description = canonical_json(
            {
                "schema": "comet-rag-vector-v1",
                "index_scope": index_scope,
                "index_profile_id": index_profile_id,
                "space_id": space.space_id,
                "dimension": space.dimension,
                "metric": space.metric,
                "field_types": {
                    key: kind.__name__ for key, kind in self._fields.items()
                },
            }
        ).decode("utf-8")

    @property
    def index_scope(self) -> str:
        return self._index_scope

    @property
    def index_profile_id(self) -> str:
        return self._index_profile_id

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def collection_name(self) -> str:
        return collection_name_for(self.index_scope)

    def _ready(self) -> None:
        if not self._prepared:
            raise RuntimeError("prepare() must succeed before using the Milvus store")

    async def prepare(self) -> None:
        try:
            await self._prepare()
        except ValueError:
            self._prepared = False
            raise

    async def _prepare(self) -> None:
        name = self.collection_name
        if not await self._client.has_collection(name, timeout=self._timeout):
            self._prepared = False
            schema = self._client.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
                description=self._description,
            )
            for field in _IDENTITY:
                schema.add_field(
                    field,
                    DataType.VARCHAR,
                    is_primary=field == "record_id",
                    max_length=128 if field in ("record_id", "fingerprint") else 65535,
                )
            schema.add_field("metadata", DataType.JSON)
            schema.add_field("dense", DataType.FLOAT_VECTOR, dim=self.space.dimension)
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            schema.add_field(
                "keyword_text",
                DataType.VARCHAR,
                max_length=65535,
                enable_analyzer=True,
                analyzer_params=_ANALYZER,
            )
            schema.add_function(
                Function(
                    name=_FUNCTION,
                    function_type=FunctionType.BM25,
                    input_field_names=["keyword_text"],
                    output_field_names=["sparse"],
                )
            )
            await self._client.create_collection(
                name,
                schema=schema,
                consistency_level="Strong",
                timeout=self._timeout,
            )
        description = cast(
            "Mapping[str, Any]",
            await self._client.describe_collection(name, timeout=self._timeout),
        )
        self._validate_schema(description)
        indexes = [
            await self._client.describe_index(name, index_name, timeout=self._timeout)
            for index_name in await self._client.list_indexes(
                name, timeout=self._timeout
            )
        ]
        missing = []
        for field, (index_name, kind) in _INDEXES.items():
            existing = [index for index in indexes if index.get("field_name") == field]
            metric = self.space.metric if field == "dense" else "BM25"
            if not existing:
                missing.append((field, index_name, kind, metric))
                continue
            if len(existing) != 1:
                raise ValueError(f"incompatible Milvus index for {field}")
            if any(
                _params(index.get("params")).get("index_type", index.get("index_type"))
                != kind
                or _params(index.get("params")).get(
                    "metric_type", index.get("metric_type")
                )
                != metric
                or index.get("state") not in (None, "Finished")
                for index in existing
            ):
                raise ValueError(f"incompatible Milvus index for {field}")
        if missing:
            self._prepared = False
            parameters = self._client.prepare_index_params()
            for field, index_name, kind, metric in missing:
                parameters.add_index(
                    field_name=field,
                    index_name=index_name,
                    index_type=kind,
                    metric_type=metric,
                )
            await self._client.create_index(name, parameters, timeout=self._timeout)
            # 重新检查真实索引描述，不能把建索引 ACK 当成已准备完成。
            for field, index_name, kind, metric in missing:
                index = await self._client.describe_index(
                    name, index_name, timeout=self._timeout
                )
                params = _params(index.get("params"))
                if index.get("field_name") != field:
                    raise ValueError(f"incompatible Milvus index for {field}")
                if params.get("index_type", index.get("index_type")) != kind:
                    raise ValueError(f"incompatible Milvus index for {field}")
                if params.get("metric_type", index.get("metric_type")) != metric:
                    raise ValueError(f"incompatible Milvus index for {field}")
                if index.get("state") not in (None, "Finished"):
                    raise ValueError(f"incompatible Milvus index for {field}")
        await self._client.load_collection(name, timeout=self._timeout)
        if (await self._client.get_load_state(name, timeout=self._timeout)).get(
            "state"
        ) != LoadState.Loaded:
            self._prepared = False
            raise RuntimeError("Milvus collection is not loaded")
        # 元数据 Loaded 可能早于重启后的分片恢复；用数据查询确认实际就绪。
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                await self._client.query(
                    name,
                    filter='record_id in [""]',
                    output_fields=["record_id"],
                    limit=1,
                    consistency_level="Strong",
                    timeout=max(0.001, deadline - time.monotonic()),
                )
                break
            except MilvusException as exc:
                self._prepared = False
                if exc.code != 503:
                    raise
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
        self._prepared = True

    def _validate_schema(self, description: Mapping[str, Any]) -> None:
        if description.get("description") != self._description:
            raise ValueError(
                "incompatible Milvus scope/profile/space or metadata configuration"
            )
        fields = {field.get("name"): field for field in description.get("fields", ())}
        expected = dict.fromkeys(_IDENTITY, DataType.VARCHAR) | {
            "keyword_text": DataType.VARCHAR,
            "metadata": DataType.JSON,
            "dense": DataType.FLOAT_VECTOR,
            "sparse": DataType.SPARSE_FLOAT_VECTOR,
        }
        if set(fields) != set(expected):
            raise ValueError("incompatible Milvus field schema")
        if any(fields[name].get("type") != kind for name, kind in expected.items()):
            raise ValueError("incompatible Milvus field schema")
        if _true(description.get("auto_id")):
            raise ValueError(
                "incompatible Milvus primary key or dynamic/nullable schema"
            )
        if _true(description.get("enable_dynamic_field")):
            raise ValueError(
                "incompatible Milvus primary key or dynamic/nullable schema"
            )
        if any(
            _true(field.get("auto_id"))
            or _true(field.get("nullable"))
            or _true(field.get("is_primary")) != (name == "record_id")
            for name, field in fields.items()
        ):
            raise ValueError(
                "incompatible Milvus primary key or dynamic/nullable schema"
            )
        for name, field in fields.items():
            if expected[name] == DataType.VARCHAR:
                length = 128 if name in ("record_id", "fingerprint") else 65535
                if str(_params(field.get("params")).get("max_length")) != str(length):
                    raise ValueError(f"incompatible Milvus string length for {name}")
        if str(_params(fields["dense"].get("params")).get("dim")) != str(
            self.space.dimension
        ):
            raise ValueError("incompatible Milvus vector dimension")
        text_params = _params(fields["keyword_text"].get("params"))
        if not _true(text_params.get("enable_analyzer")):
            raise ValueError("incompatible Milvus BM25 analyzer")
        if _params(text_params.get("analyzer_params")) != _ANALYZER:
            raise ValueError("incompatible Milvus BM25 analyzer")
        functions = description.get("functions") or []
        if len(functions) != 1:
            raise ValueError("incompatible Milvus BM25 function")
        if not any(
            function.get("name") == _FUNCTION
            and function.get("type") in (FunctionType.BM25, "BM25")
            and function.get("input_field_names") == ["keyword_text"]
            and function.get("output_field_names") == ["sparse"]
            for function in functions
        ):
            raise ValueError("incompatible Milvus BM25 function")

    def _encode_rows(self, records: tuple[VectorRecord, ...]) -> list[dict[str, Any]]:
        rows = []
        for record in records:
            row = {name: getattr(record, name) for name in _IDENTITY}
            metadata = canonical_json(record.metadata)
            if len(metadata) > 65536:
                raise ValueError("record exceeds Milvus field limits")
            if len(record.keyword_text.encode("utf-8")) > 65535:
                raise ValueError("record exceeds Milvus field limits")
            if any(
                len(value.encode("utf-8"))
                > (128 if key in ("record_id", "fingerprint") else 65535)
                for key, value in row.items()
            ):
                raise ValueError("record exceeds Milvus field limits")
            if any(abs(value) > _MAX_FLOAT32 for value in record.vector):
                raise ValueError("record exceeds Milvus field limits")
            row.update(
                metadata=json.loads(metadata),
                dense=list(record.vector),
                keyword_text=record.keyword_text,
            )
            rows.append(row)
        return rows

    async def _lookup(
        self, ids: tuple[str, ...]
    ) -> dict[str, Mapping[str, Any] | None]:
        found: dict[str, Mapping[str, Any] | None] = {}
        for offset in range(0, len(ids), 1000):
            batch = ids[offset : offset + 1000]
            rows = await self._client.query(
                self.collection_name,
                filter=f"record_id in {json.dumps(batch, ensure_ascii=False)}",
                output_fields=list(_IDENTITY),
                consistency_level="Strong",
                timeout=self._timeout,
            )
            for row in rows:
                key = row.get("record_id")
                if not isinstance(key, str):
                    raise RuntimeError("Milvus returned an unexpected record identity")
                if key not in batch:
                    raise RuntimeError("Milvus returned an unexpected record identity")
                found[key] = row if key not in found else None
        return found

    def _valid_identity(self, record_id: str, row: Mapping[str, Any] | None) -> bool:
        if row is None or any(
            not isinstance(row.get(name), str) or not row[name].strip()
            for name in _IDENTITY
        ):
            return False
        return (
            row["record_id"] == record_id
            and row["index_scope"] == self.index_scope
            and row["index_profile_id"] == self.index_profile_id
            and row["space_id"] == self.space.space_id
            and record_id
            == stable_id(
                "record",
                {
                    "scope": self.index_scope,
                    "representation": row["representation_id"],
                    "space": self.space.space_id,
                },
            )
            and re.fullmatch(r"record_content_[0-9a-f]{64}", row["fingerprint"])
            is not None
        )

    async def _verify(
        self, ids: tuple[str, ...], fingerprints: Mapping[str, str] | None
    ) -> RecordVerification:
        rows = await self._lookup(ids)
        present, missing, unknown = [], [], []
        for key in ids:
            row = rows.get(key)
            if key not in rows:
                missing.append(key)
            elif not self._valid_identity(key, row) or (
                fingerprints is not None
                and row is not None
                and row["fingerprint"] != fingerprints[key]
            ):
                unknown.append(key)
            else:
                present.append(key)
        return RecordVerification(
            tuple(present),
            tuple(missing),
            tuple(unknown),
            "Milvus Strong identity/fingerprint query",
        )

    async def upsert(self, records: Sequence[VectorRecord]) -> VectorConfirmation:
        snapshots = _snapshot_records(
            records, self.index_scope, self.index_profile_id, self.space, self._fields
        )
        rows = self._encode_rows(snapshots)
        self._ready()
        ids = tuple(record.record_id for record in snapshots)
        existing = await self._lookup(ids)
        for row in rows:
            key = row["record_id"]
            previous = existing.get(key)
            if key in existing:
                if not self._valid_identity(key, previous):
                    raise ValueError("conflicting content for the same record ID")
                if any(
                    previous is None or previous.get(name) != row[name]
                    for name in _IDENTITY
                ):
                    raise ValueError("conflicting content for the same record ID")
        new_rows = [row for row in rows if row["record_id"] not in existing]
        accepted = True
        if new_rows:
            response = await self._client.upsert(
                self.collection_name, new_rows, timeout=self._timeout
            )
            accepted = response.get("upsert_count") == len(new_rows)
        if ids:
            await self._client.flush(self.collection_name, timeout=self._timeout)
        verification = await self._verify(
            ids, {record.record_id: record.fingerprint for record in snapshots}
        )
        unknown = (
            tuple(key for key in ids if key not in verification.present)
            if accepted
            else ids
        )
        return VectorConfirmation(
            ids,
            accepted=accepted,
            durable=accepted,
            query_visible=accepted and not unknown,
            unknown_ids=unknown,
            guarantee="Milvus upsert ACK + flush + Strong identity/fingerprint query",
        )

    async def search(
        self, query: EmbeddingResult, *, limit: int = 40, filters: Filter | None = None
    ) -> tuple[VectorHit, ...]:
        _query(query, self.space)
        _limit(limit)
        validate_filter(filters, self._fields)
        self._ready()
        return await self._search(
            [list(query.vector)],
            "dense",
            self.space.metric,
            limit,
            expression(filters),
        )

    async def search_text(
        self, text: str, *, limit: int = 40, filters: Filter | None = None
    ) -> tuple[VectorHit, ...]:
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        _limit(limit)
        validate_filter(filters, self._fields)
        self._ready()
        if not text.strip():
            return ()
        return await self._search([text], "sparse", "BM25", limit, expression(filters))

    async def _search(
        self, data: list[Any], field: str, metric: str, limit: int, predicate: str
    ) -> tuple[VectorHit, ...]:
        binding = " and ".join(
            f"{name} == {json.dumps(value, ensure_ascii=False)}"
            for name, value in (
                ("index_scope", self.index_scope),
                ("index_profile_id", self.index_profile_id),
                ("space_id", self.space.space_id),
            )
        )
        result = await self._client.search(
            self.collection_name,
            data=data,
            anns_field=field,
            search_params={"metric_type": metric},
            filter=f"({binding}) and ({predicate})" if predicate else binding,
            limit=limit,
            output_fields=list(_IDENTITY),
            consistency_level="Strong",
            timeout=self._timeout,
        )
        hits = []
        seen: set[str] = set()
        for item in result[0] if result else []:
            row = item.get("entity", {}) | {"record_id": item.get("record_id")}
            if not self._valid_identity(row["record_id"], row):
                raise RuntimeError("Milvus returned an inconsistent record identity")
            if row["record_id"] in seen:
                raise RuntimeError("Milvus returned a duplicate record hit")
            seen.add(row["record_id"])
            score = item.get("distance")
            if isinstance(score, bool):
                raise RuntimeError("Milvus returned a nonnumeric score")
            if not isinstance(score, (int, float)):
                raise RuntimeError("Milvus returned a nonnumeric score")
            score = float(score)
            if not math.isfinite(score):
                raise RuntimeError("Milvus returned a nonfinite score")
            hits.append(
                VectorHit(
                    **{name: row[name] for name in _IDENTITY if name != "fingerprint"},
                    score=score,
                    metric=metric,
                    higher_is_better=metric != "L2",
                )
            )
        hits.sort(
            key=lambda hit: (
                (-hit.score if hit.higher_is_better else hit.score),
                hit.record_id,
            )
        )
        return tuple(hits)

    async def verify_records(
        self,
        record_ids: Sequence[str],
        *,
        fingerprints: Mapping[str, str] | None = None,
    ) -> RecordVerification:
        ids = _ids(record_ids, fingerprints)
        expected = dict(fingerprints) if fingerprints is not None else None
        self._ready()
        return await self._verify(ids, expected)

    async def delete(self, record_ids: Sequence[str]) -> VectorConfirmation:
        ids = _ids(record_ids)
        self._ready()
        for offset in range(0, len(ids), 1000):
            await self._client.delete(
                self.collection_name,
                ids=list(ids[offset : offset + 1000]),
                timeout=self._timeout,
            )
        if ids:
            await self._client.flush(self.collection_name, timeout=self._timeout)
        verification = await self._verify(ids, None)
        unknown = tuple(key for key in ids if key not in verification.missing)
        return VectorConfirmation(
            ids,
            accepted=True,
            durable=True,
            query_visible=not unknown,
            unknown_ids=unknown,
            guarantee="Milvus delete ACK + flush + Strong absence query",
        )


__all__ = ["AsyncMilvusVectorStore", "collection_name_for"]
