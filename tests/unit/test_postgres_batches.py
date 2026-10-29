from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects.postgresql import asyncpg

from comet_rag.docstores.postgres import AsyncPostgresDocStore


def test_large_id_queries_bind_arrays_without_expanding_parameters() -> None:
    async def run() -> None:
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        result.first.return_value = None
        connection = MagicMock()
        connection.execute = AsyncMock(return_value=result)
        engine = MagicMock()
        for method in (engine.begin, engine.connect):
            method.return_value.__aenter__ = AsyncMock(return_value=connection)
            method.return_value.__aexit__ = AsyncMock(return_value=False)
        store = AsyncPostgresDocStore(index_scope="scope", engine=engine)
        keys = tuple(f"unit-{i}" for i in range(32768))
        assert await store.get_units(keys) == {}
        assert await store.get_representations(keys) == {}
        await store.delete_units(keys)
        assert connection.execute.await_count == 6
        for call in connection.execute.await_args_list:
            compiled = call.args[0].compile(
                dialect=asyncpg.dialect(),
                compile_kwargs={"render_postcompile": True},
            )
            assert len(compiled.positiontup) <= 4
            arrays = [
                value for value in compiled.params.values() if isinstance(value, list)
            ]
            assert arrays and all(set(value) == set(keys) for value in arrays)

    asyncio.run(run())
