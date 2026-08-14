"""集成测试夹具。

**中间件没起时跳过，而不是失败。** 集成测试是可选的：核心依赖环境（CI 的
core-only job、只想跑单元测试的贡献者）根本没有 docker，让它们红一片
只会训练出"看到红色就忽略"的习惯，那比没有测试更糟。

起中间件：`docker compose up -d`
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.integration

#: 与 docker-compose.yml 一致。可用环境变量覆盖，方便对着别的实例跑。
POSTGRES_DSN = os.environ.get(
    "COMET_TEST_POSTGRES_DSN",
    "postgresql+asyncpg://comet:comet@localhost:5432/comet_rag",
)
REDIS_URL = os.environ.get("COMET_TEST_REDIS_URL", "redis://localhost:6379/0")
MILVUS_URI = os.environ.get("COMET_TEST_MILVUS_URI", "http://localhost:19530")
MINIO_ENDPOINT = os.environ.get("COMET_TEST_MINIO_ENDPOINT", "http://localhost:9010")


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """只探端口，不建真连接 —— 探测本身不该因为库没装而失败。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _require(host: str, port: int, name: str) -> None:
    if not _port_open(host, port):
        pytest.skip(f"{name} 未运行（{host}:{port}）。先跑 `docker compose up -d`")


def _endpoint_address(endpoint: str) -> tuple[str, int]:
    """Return the socket address represented by an HTTP(S) service endpoint."""

    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"endpoint 必须是包含主机名的 http/https URL：{endpoint!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint 不得包含凭据")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"endpoint 端口无效：{endpoint!r}") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, port


def _require_endpoint(endpoint: str, name: str) -> None:
    host, port = _endpoint_address(endpoint)
    _require(host, port, name)


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    _require("localhost", 5432, "PostgreSQL")
    return POSTGRES_DSN


@pytest.fixture(scope="session")
def redis_url() -> str:
    _require("localhost", 6379, "Redis")
    return REDIS_URL


@pytest.fixture(scope="session")
def milvus_uri() -> str:
    _require("localhost", 19530, "Milvus")
    return MILVUS_URI


@pytest.fixture(scope="session")
def minio_endpoint() -> str:
    try:
        _require_endpoint(MINIO_ENDPOINT, "MinIO")
    except ValueError as exc:
        pytest.fail(f"COMET_TEST_MINIO_ENDPOINT 配置无效：{exc}", pytrace=False)
    return MINIO_ENDPOINT


#: `TRUNCATE` 要 ACCESS EXCLUSIVE 锁。有别的连接还开着事务时，
#: 默认行为是**无限期等下去** —— 症状就是整个测试进程静默挂起，没有任何输出，
#: 排查时甚至看不出卡在哪个用例上。曾经真的挂过 11 分钟才被人工掐掉。
#: 加上超时，就把"静默挂起"换成了一条指名道姓的报错。
LOCK_TIMEOUT = "5s"


async def truncate_tables(target: Any, *tables: str) -> None:
    """清表。表不存在则 skip（提示先跑迁移），拿不到锁则**报错而不是干等**。

    `target` 可以是 DSN 字符串（自建自弃一个引擎），也可以是现成的 `Database`。
    """
    from comet_rag.infrastructure.database import Database

    own = isinstance(target, str)
    db = Database(target) if own else target
    try:
        async with db.session() as session:
            for table in tables:
                exists = (
                    await session.execute(text(f"SELECT to_regclass('{table}')"))
                ).scalar_one()
                if exists is None:
                    pytest.skip(f"{table} 表不存在，先跑 `uv run alembic upgrade head`")
        async with db.transaction() as session:
            await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            for table in tables:
                # CASCADE：一并清掉靠外键挂着的行（如 task_events）
                await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    except OperationalError as exc:  # lock_timeout 到点
        pytest.fail(
            f"清 {tables} 时在 {LOCK_TIMEOUT} 内拿不到锁 —— "
            f"多半是上一个用例漏关了会话或事务：{exc}"
        )
    finally:
        if own:
            await db.aclose()


@pytest.fixture
async def database(postgres_dsn: str) -> AsyncIterator:
    """独立引擎，用完即弃。

    每个用例一个引擎有点浪费，但集成测试量不大，而共享引擎会让"某个用例
    没关干净事务"变成后续用例的随机失败 —— 那种问题排查成本极高。
    """
    from comet_rag.infrastructure.database import Database

    db = Database(postgres_dsn)
    try:
        yield db
    finally:
        await db.aclose()
