from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from hashlib import sha256

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    all_,
    any_,
    bindparam,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSON
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.schema import CreateSchema

from comet_rag.indexing.types import IndexUnit, Representation
from comet_rag.vectorstores.types import VectorConfirmation

from .base import (
    _batch_size,
    _content_payload,
    _decode_content,
    _ids,
    _scope,
    _snapshot,
    _validate_manifest_match,
    _validate_unit_parents,
)
from .types import WriteManifest, content_digest, manifest_from_dict, manifest_to_dict


class AsyncPostgresDocStore:
    """显式 prepare 创建独立表；注入的 engine 始终由创建者关闭。"""

    def __init__(
        self,
        *,
        index_scope: str,
        engine: AsyncEngine,
        schema: str = "comet_docstore_v2",
    ) -> None:
        self._index_scope = _scope(index_scope)
        if not isinstance(schema, str):
            raise ValueError(
                "schema must be a PostgreSQL identifier of at most 63 ASCII characters"
            )
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema) is None:
            raise ValueError(
                "schema must be a PostgreSQL identifier of at most 63 ASCII characters"
            )
        self._engine = engine
        self._metadata = MetaData(schema=schema)
        self._manifests = Table(
            "docstore_manifests",
            self._metadata,
            Column("index_scope", Text, primary_key=True),
            Column("document_id", Text, primary_key=True),
            Column("source_id", Text, nullable=False),
            Column("index_profile_id", Text, nullable=False),
            Column("manifest_id", Text, nullable=False),
            Column("content_fingerprint", Text, nullable=False),
            Column("payload", JSON, nullable=False),
            UniqueConstraint("index_scope", "source_id"),
            UniqueConstraint("index_scope", "manifest_id"),
            UniqueConstraint(
                "index_scope", "source_id", "document_id", "index_profile_id"
            ),
            CheckConstraint(
                "payload ->> 'type' = 'manifest' AND payload ->> 'schema_version' = '2'"
            ),
        )
        self._units = Table(
            "docstore_units",
            self._metadata,
            Column("index_scope", Text, primary_key=True),
            Column("unit_id", Text, primary_key=True),
            Column("source_id", Text, nullable=False),
            Column("document_id", Text, nullable=False),
            Column("index_profile_id", Text, nullable=False),
            Column("parent_unit_id", Text),
            Column("payload", JSON, nullable=False),
            UniqueConstraint(
                "index_scope", "source_id", "document_id", "index_profile_id", "unit_id"
            ),
            ForeignKeyConstraint(
                ["index_scope", "source_id", "document_id", "index_profile_id"],
                [
                    self._manifests.c[name]
                    for name in (
                        "index_scope",
                        "source_id",
                        "document_id",
                        "index_profile_id",
                    )
                ],
                deferrable=True,
                initially="DEFERRED",
            ),
            ForeignKeyConstraint(
                [
                    "index_scope",
                    "source_id",
                    "document_id",
                    "index_profile_id",
                    "parent_unit_id",
                ],
                [
                    f"{schema}.docstore_units.{name}"
                    for name in (
                        "index_scope",
                        "source_id",
                        "document_id",
                        "index_profile_id",
                        "unit_id",
                    )
                ],
                deferrable=True,
                initially="DEFERRED",
            ),
            CheckConstraint(
                "payload ->> 'type' = 'unit' AND payload ->> 'schema_version' = '1'"
            ),
        )
        self._representations = Table(
            "docstore_representations",
            self._metadata,
            Column("index_scope", Text, primary_key=True),
            Column("representation_id", Text, primary_key=True),
            Column("unit_id", Text, nullable=False),
            Column("source_id", Text, nullable=False),
            Column("document_id", Text, nullable=False),
            Column("index_profile_id", Text, nullable=False),
            Column("payload", JSON, nullable=False),
            ForeignKeyConstraint(
                [
                    "index_scope",
                    "source_id",
                    "document_id",
                    "index_profile_id",
                    "unit_id",
                ],
                [
                    self._units.c[name]
                    for name in (
                        "index_scope",
                        "source_id",
                        "document_id",
                        "index_profile_id",
                        "unit_id",
                    )
                ],
                deferrable=True,
                initially="DEFERRED",
            ),
            CheckConstraint(
                "payload ->> 'type' = 'representation' AND payload ->> 'schema_version' = '1'"
            ),
        )
        Index(
            "docstore_units_document",
            self._units.c.index_scope,
            self._units.c.document_id,
        )
        Index(
            "docstore_units_parent",
            self._units.c.index_scope,
            self._units.c.parent_unit_id,
        )
        Index(
            "docstore_representations_unit",
            self._representations.c.index_scope,
            self._representations.c.unit_id,
        )

    @property
    def index_scope(self) -> str:
        return self._index_scope

    async def prepare(self) -> None:
        async with self._engine.begin() as connection:
            # DDL 同样串行，避免两个显式初始化同时通过 checkfirst。
            await self._lock(connection, "prepare", str(self._metadata.schema))
            await connection.execute(
                CreateSchema(str(self._metadata.schema), if_not_exists=True)
            )
            await connection.run_sync(self._metadata.create_all)

    async def _lock(self, connection: AsyncConnection, kind: str, value: str) -> None:
        key = int.from_bytes(
            sha256(
                f"{self._metadata.schema}\0{kind}\0{self.index_scope if kind != 'prepare' else ''}\0{value}".encode()
            ).digest()[:8],
            signed=True,
        )
        await connection.execute(select(func.pg_advisory_xact_lock(key)))

    def _decode[T: IndexUnit | Representation](
        self, row: RowMapping, cls: type[T]
    ) -> T:
        value = _decode_content(row["payload"])
        names = (
            "index_scope",
            "source_id",
            "document_id",
            "index_profile_id",
            "unit_id",
        )
        if cls is Representation:
            names += ("representation_id",)
        else:
            names += ("parent_unit_id",)
        if not isinstance(value, cls):
            raise ValueError("stored content identity mismatch")
        if value.index_scope != self.index_scope:
            raise ValueError("stored content identity mismatch")
        if any(getattr(value, name) != row[name] for name in names):
            raise ValueError("stored content identity mismatch")
        return value

    def _decode_manifest_row(self, row: RowMapping) -> WriteManifest:
        value = manifest_from_dict(row["payload"])
        if value.index_scope != self.index_scope:
            raise ValueError("stored manifest identity mismatch")
        if any(
            getattr(value, name) != row[name]
            for name in (
                "index_scope",
                "source_id",
                "document_id",
                "index_profile_id",
                "manifest_id",
                "content_fingerprint",
            )
        ):
            raise ValueError("stored manifest identity mismatch")
        return value

    async def _read_units(
        self, connection: AsyncConnection, ids: Sequence[str]
    ) -> dict[str, IndexUnit]:
        requested = _ids(ids)
        if not requested:
            return {}
        result: dict[str, IndexUnit] = {}
        pending = set(requested)
        first = True
        while pending:
            rows = (
                (
                    await connection.execute(
                        select(self._units).where(
                            self._units.c.index_scope == self.index_scope,
                            self._units.c.unit_id
                            == any_(
                                bindparam("unit_ids", list(pending), type_=ARRAY(Text))
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            found: set[str] = set()
            for row in rows:
                value = self._decode(row, IndexUnit)
                if not isinstance(value, IndexUnit):
                    raise ValueError("invalid unit type")
                found.add(value.unit_id)
                result[value.unit_id] = value
            if not first:
                if found != pending:
                    raise ValueError("missing parent")
            pending = {
                unit.parent_unit_id
                for unit in result.values()
                if unit.parent_unit_id is not None and unit.parent_unit_id not in result
            }
            first = False
        _validate_unit_parents(result)
        return {key: result[key] for key in requested if key in result}

    async def upsert_units(
        self,
        units: Sequence[IndexUnit],
        *,
        representations: Sequence[Representation],
        manifest: WriteManifest,
    ) -> None:
        units, representations, manifest = _snapshot(
            self.index_scope, units, representations, manifest
        )
        async with self._engine.begin() as connection:
            await self._lock(connection, "source", manifest.source_id)
            await self._lock(connection, "document", manifest.document_id)
            rows = (
                (
                    await connection.execute(
                        select(self._manifests).where(
                            self._manifests.c.index_scope == self.index_scope,
                            (self._manifests.c.source_id == manifest.source_id)
                            | (self._manifests.c.document_id == manifest.document_id),
                        )
                    )
                )
                .mappings()
                .all()
            )
            if rows:
                for row in rows:
                    _validate_manifest_match(self._decode_manifest_row(row), manifest)
                stored_units = await self._read_units(connection, manifest.unit_ids)
                rep_rows = (
                    (
                        await connection.execute(
                            select(self._representations).where(
                                self._representations.c.index_scope == self.index_scope,
                                self._representations.c.document_id
                                == manifest.document_id,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                stored_reps = {
                    row["representation_id"]: self._decode(row, Representation)
                    for row in rep_rows
                }
                if set(stored_units) != set(manifest.unit_ids):
                    raise ValueError(
                        "stored manifest content is incomplete or inconsistent"
                    )
                if set(stored_reps) != set(manifest.representation_ids):
                    raise ValueError(
                        "stored manifest content is incomplete or inconsistent"
                    )
                if (
                    content_digest(
                        [stored_units[key] for key in manifest.unit_ids],
                        [stored_reps[key] for key in manifest.representation_ids],
                    )
                    != manifest.content_fingerprint
                ):
                    raise ValueError(
                        "stored manifest content is incomplete or inconsistent"
                    )
                return
            if units:
                await connection.execute(
                    self._units.insert(),
                    [
                        {
                            "index_scope": self.index_scope,
                            "unit_id": unit.unit_id,
                            "source_id": unit.source_id,
                            "document_id": unit.document_id,
                            "index_profile_id": unit.index_profile_id,
                            "parent_unit_id": unit.parent_unit_id,
                            "payload": _content_payload(unit),
                        }
                        for unit in units
                    ],
                )
            if representations:
                await connection.execute(
                    self._representations.insert(),
                    [
                        {
                            "index_scope": self.index_scope,
                            "representation_id": rep.representation_id,
                            "unit_id": rep.unit_id,
                            "source_id": rep.source_id,
                            "document_id": rep.document_id,
                            "index_profile_id": rep.index_profile_id,
                            "payload": _content_payload(rep),
                        }
                        for rep in representations
                    ],
                )
            await connection.execute(
                self._manifests.insert().values(
                    index_scope=self.index_scope,
                    document_id=manifest.document_id,
                    source_id=manifest.source_id,
                    index_profile_id=manifest.index_profile_id,
                    manifest_id=manifest.manifest_id,
                    content_fingerprint=manifest.content_fingerprint,
                    payload=manifest_to_dict(manifest),
                )
            )

    async def get_units(self, ids: Sequence[str]) -> dict[str, IndexUnit]:
        keys = _ids(ids)
        async with self._engine.connect() as connection:
            return await self._read_units(connection, keys)

    async def get_representations(
        self, ids: Sequence[str]
    ) -> dict[str, Representation]:
        keys = _ids(ids)
        if not keys:
            return {}
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(self._representations).where(
                            self._representations.c.index_scope == self.index_scope,
                            self._representations.c.representation_id
                            == any_(
                                bindparam(
                                    "representation_ids", list(keys), type_=ARRAY(Text)
                                )
                            ),
                        )
                    )
                )
                .mappings()
                .all()
            )
            result: dict[str, Representation] = {}
            for row in rows:
                rep = self._decode(row, Representation)
                if not isinstance(rep, Representation):
                    raise ValueError("invalid representation type")
                result[rep.representation_id] = rep
            units = await self._read_units(
                connection, tuple(rep.unit_id for rep in result.values())
            )
            for rep in result.values():
                unit = units.get(rep.unit_id)
                if unit is None:
                    raise ValueError("representation unit identity mismatch")
                if (rep.source_id, rep.document_id, rep.index_profile_id) != (
                    unit.source_id,
                    unit.document_id,
                    unit.index_profile_id,
                ):
                    raise ValueError("representation unit identity mismatch")
            return result

    async def get_manifest(self, document_id: str) -> WriteManifest | None:
        _ids((document_id,))
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(self._manifests).where(
                            self._manifests.c.index_scope == self.index_scope,
                            self._manifests.c.document_id == document_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            return self._decode_manifest_row(row) if row is not None else None

    async def iter_manifests(
        self, document_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[WriteManifest]:
        _batch_size(batch_size)
        if document_id is not None:
            manifest = await self.get_manifest(document_id)
            if manifest is not None:
                yield manifest
            return
        after = ""
        while True:
            async with self._engine.connect() as connection:
                rows = (
                    (
                        await connection.execute(
                            select(self._manifests)
                            .where(
                                self._manifests.c.index_scope == self.index_scope,
                                self._manifests.c.document_id > after,
                            )
                            .order_by(self._manifests.c.document_id)
                            .limit(batch_size)
                        )
                    )
                    .mappings()
                    .all()
                )
                batch = [self._decode_manifest_row(row) for row in rows]
            if not batch:
                return
            for manifest in batch:
                yield manifest
            after = batch[-1].document_id

    async def record_vector_confirmation(
        self, document_id: str, confirmation: VectorConfirmation
    ) -> None:
        _ids((document_id,))
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        select(self._manifests)
                        .where(
                            self._manifests.c.index_scope == self.index_scope,
                            self._manifests.c.document_id == document_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("manifest does not exist")
            manifest = self._decode_manifest_row(row).with_confirmation(confirmation)
            await connection.execute(
                update(self._manifests)
                .where(
                    self._manifests.c.index_scope == self.index_scope,
                    self._manifests.c.document_id == document_id,
                )
                .values(payload=manifest_to_dict(manifest))
            )

    async def delete_units(self, ids: Sequence[str]) -> None:
        keys = _ids(ids)
        if not keys:
            return
        id_array = bindparam("unit_ids", list(keys), type_=ARRAY(Text))
        async with self._engine.begin() as connection:
            units = await self._read_units(connection, keys)
            for source_id in sorted({unit.source_id for unit in units.values()}):
                await self._lock(connection, "source", source_id)
            surviving = (
                await connection.execute(
                    select(self._units.c.unit_id)
                    .where(
                        self._units.c.index_scope == self.index_scope,
                        self._units.c.parent_unit_id == any_(id_array),
                        self._units.c.unit_id != all_(id_array),
                    )
                    .limit(1)
                )
            ).first()
            if surviving is not None:
                raise ValueError("cannot delete a parent referenced by surviving units")
            await connection.execute(
                delete(self._representations).where(
                    self._representations.c.index_scope == self.index_scope,
                    self._representations.c.unit_id == any_(id_array),
                )
            )
            await connection.execute(
                delete(self._units).where(
                    self._units.c.index_scope == self.index_scope,
                    self._units.c.unit_id == any_(id_array),
                )
            )

    async def delete_manifest(self, document_id: str) -> None:
        _ids((document_id,))
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        select(self._manifests).where(
                            self._manifests.c.index_scope == self.index_scope,
                            self._manifests.c.document_id == document_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            manifest = self._decode_manifest_row(row)
            await self._lock(connection, "source", manifest.source_id)
            await self._lock(connection, "document", document_id)
            for table in (self._units, self._representations):
                if (
                    await connection.execute(
                        select(table.c.document_id)
                        .where(
                            table.c.index_scope == self.index_scope,
                            table.c.document_id == document_id,
                        )
                        .limit(1)
                    )
                ).first() is not None:
                    raise ValueError("cannot delete manifest while content remains")
            await connection.execute(
                delete(self._manifests).where(
                    self._manifests.c.index_scope == self.index_scope,
                    self._manifests.c.document_id == document_id,
                )
            )
