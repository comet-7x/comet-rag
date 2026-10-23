from __future__ import annotations

import pytest

from tests.integration.worker_config import make_worker_config


def test_worker_config_uses_probed_endpoints_and_decodes_credentials() -> None:
    config = make_worker_config(
        "postgresql+asyncpg://test_user:p%40ss%2Fword@192.0.2.10:25432/test_v2",
        "redis://:r%40ss%2Fword@192.0.2.11:26379/3",
        redis_db=15,
    )
    database, redis = (
        config.infrastructure_config.database,
        config.infrastructure_config.redis,
    )
    assert database is not None and redis is not None
    assert (
        database.dsn
        == "postgresql+asyncpg://test_user:p%40ss%2Fword@192.0.2.10:25432/test_v2"
    )
    assert redis.url == "redis://:r%40ss%2Fword@192.0.2.11:26379/15"


@pytest.mark.parametrize(
    "url", ["rediss://cache.test/0", "redis://user:password@cache.test/0"]
)
def test_worker_config_rejects_unrepresentable_redis_settings(url: str) -> None:
    with pytest.raises(ValueError, match="不能表达"):
        make_worker_config(
            "postgresql://test:password@database.test/test", url, redis_db=15
        )


@pytest.mark.parametrize("failed", [False, True])
def test_milvus_smoke_uses_test_database_and_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
) -> None:
    from tests.integration.test_infrastructure import test_milvus_reachable

    created: list[dict[str, object]] = []
    closed: list[bool] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def list_collections(self, **kwargs: object) -> list[str]:
            if failed:
                raise RuntimeError("RPC failed")
            return []

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("pymilvus.MilvusClient", Client)
    if failed:
        with pytest.raises(RuntimeError, match="RPC failed"):
            test_milvus_reachable("http://milvus.test", "zhihao_test_database")
    else:
        test_milvus_reachable("http://milvus.test", "zhihao_test_database")
    assert created == [
        {"uri": "http://milvus.test", "db_name": "zhihao_test_database", "timeout": 3.0}
    ]
    assert closed == [True]


@pytest.mark.parametrize("body_fails", [False, True])
async def test_worker_fixture_drains_all_jobs_then_closes_sdk_pools(
    body_fails: bool,
) -> None:
    import asyncio
    from types import SimpleNamespace
    from typing import cast

    from arq.worker import Worker

    from tests.integration.test_e2e_workers import _running

    release = asyncio.Event()
    closed: list[int] = []
    jobs = [asyncio.create_task(release.wait()) for _ in range(2)]

    async def poll() -> None:
        await release.wait()

    async def close(index: int) -> None:
        assert all(job.done() for job in jobs), "所有 worker 的 job 应先排干"
        closed.append(index)

    async def close_first() -> None:
        await close(0)

    async def close_second() -> None:
        await close(1)

    workers = [
        cast(
            Worker,
            SimpleNamespace(
                async_run=poll, tasks={"job": jobs[0]}, close=close_first, ctx={}
            ),
        ),
        cast(
            Worker,
            SimpleNamespace(
                async_run=poll, tasks={"job": jobs[1]}, close=close_second, ctx={}
            ),
        ),
    ]
    error = RuntimeError("test body failed")
    try:
        if body_fails:
            with pytest.raises(RuntimeError) as caught:
                async with _running(*workers):
                    raise error
            assert caught.value is error
        else:
            async with _running(*workers):
                await asyncio.sleep(0)
        assert closed == [0, 1]
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
