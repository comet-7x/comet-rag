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

import pytest

pytestmark = pytest.mark.integration

#: 与 docker-compose.yml 一致。可用环境变量覆盖，方便对着别的实例跑。
POSTGRES_DSN = os.environ.get(
    "COMET_TEST_POSTGRES_DSN",
    "postgresql+asyncpg://comet:comet@localhost:5432/comet_rag",
)
REDIS_URL = os.environ.get("COMET_TEST_REDIS_URL", "redis://localhost:6379/0")
MILVUS_URI = os.environ.get("COMET_TEST_MILVUS_URI", "http://localhost:19530")


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
