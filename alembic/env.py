"""Alembic 环境。

**DSN 从 `config.yaml` 读，不写进 `alembic.ini`** —— ini 是要提交的文件，
把数据库密码写进去迟早会误提交。`sqlalchemy.url` 在 ini 里留空，
由这里在运行时填入。

`target_metadata` 指向 `Base.metadata`，`--autogenerate` 才能对比出差异。
新增的 ORM 模型必须被下面的 import 覆盖到，否则 autogenerate 会以为
那张表"该删掉"——这是 Alembic 最容易踩的坑。
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from comet_rag.config.settings import get_config
from comet_rag.infrastructure.database import Base

# 只为让 Base.metadata 收集到全部表定义。新增模型模块务必加进来。
from comet_rag.infrastructure.database import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """优先用 alembic.ini / -x 传入的值，否则回落到应用配置。"""
    override = context.get_x_argument(as_dictionary=True).get("dsn")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url", "")
    if configured:
        return configured

    app_config = get_config()
    if app_config.infrastructure_config.database is None:
        raise RuntimeError(
            "config.yaml 未配置 infrastructure_config.database，"
            "无法确定迁移目标。也可用 `alembic -x dsn=... upgrade head` 显式指定。"
        )
    return app_config.infrastructure_config.database.dsn


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 类型变更默认不被 autogenerate 检出，开了才会提示
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
