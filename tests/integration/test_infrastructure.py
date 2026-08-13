"""中间件接线冒烟：连得上、事务语义正确、迁移链路可用。

这些不测业务逻辑，只测"地基是通的"。T20/T21 的实现测试建立在此之上 ——
地基不通时它们会报出一堆看不懂的错，不如先在这里失败得明明白白。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from comet_rag.infrastructure.database import Database

pytestmark = pytest.mark.integration


# ── PostgreSQL ─────────────────────────────────────────────────────────────


async def test_database_connects(database: Database) -> None:
    async with database.session() as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_session_does_not_autocommit(database: Database) -> None:
    """`session()` 正常退出**不提交** —— 隐式提交会让"只是读一下"的代码
    在出错时写进半截数据。"""
    async with database.session() as session:
        await session.execute(text("CREATE TEMP TABLE probe (v int)"))
        await session.execute(text("INSERT INTO probe VALUES (1)"))
        # 未 commit

    async with database.session() as session:
        # TEMP 表随会话消失，能查到才说明上一个会话被隐式提交了
        exists = (
            await session.execute(text("SELECT to_regclass('pg_temp.probe')"))
        ).scalar_one()
        assert exists is None


async def test_transaction_commits_on_success(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(text("CREATE TABLE IF NOT EXISTS probe_commit (v int)"))
        await session.execute(text("INSERT INTO probe_commit VALUES (42)"))

    try:
        async with database.session() as session:
            value = (
                await session.execute(text("SELECT v FROM probe_commit"))
            ).scalar_one()
            assert value == 42
    finally:
        async with database.transaction() as session:
            await session.execute(text("DROP TABLE IF EXISTS probe_commit"))


async def test_transaction_rolls_back_on_error(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(text("CREATE TABLE IF NOT EXISTS probe_rb (v int)"))

    with pytest.raises(RuntimeError):
        async with database.transaction() as session:
            await session.execute(text("INSERT INTO probe_rb VALUES (1)"))
            raise RuntimeError("业务失败")

    try:
        async with database.session() as session:
            count = (
                await session.execute(text("SELECT count(*) FROM probe_rb"))
            ).scalar_one()
            assert count == 0, "异常路径必须回滚，否则会留下半截数据"
    finally:
        async with database.transaction() as session:
            await session.execute(text("DROP TABLE IF EXISTS probe_rb"))


async def test_engine_pools_connections(postgres_dsn: str) -> None:
    """连接池要真的复用 —— 每次新建连接是 spec S4-4 的同款浪费。"""
    db = Database(postgres_dsn, pool_size=2)
    try:
        async with db.session() as s1:
            await s1.execute(text("SELECT 1"))
        async with db.session() as s2:
            await s2.execute(text("SELECT 1"))

        # `checkedin()` 只在 QueuePool 上有 —— 基类 `Pool` 没有这个概念。
        # 断言前先确认拿到的确实是带池的那种，否则本用例等于什么都没测。
        pool = db.engine.pool
        assert isinstance(pool, QueuePool), f"没有用连接池：{type(pool).__name__}"
        assert pool.checkedin() >= 1, "连接用完应归还池中而非丢弃"
    finally:
        await db.aclose()


# ── Milvus ─────────────────────────────────────────────────────────────────


def test_milvus_reachable(milvus_uri: str) -> None:
    from pymilvus import MilvusClient

    client = MilvusClient(uri=milvus_uri)

    assert isinstance(client.list_collections(), list)


# ── Redis ──────────────────────────────────────────────────────────────────


async def test_redis_reachable(redis_url: str) -> None:
    redis = pytest.importorskip("redis.asyncio", reason="需要 redis 客户端")

    client = redis.from_url(redis_url)
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()
