"""异步引擎与会话工厂。

引擎是**应用级单例**：它持有连接池，每个请求新建一个等于没有池化
（spec S4-4 的同款问题）。故由 `Context` 持有、`aclose()` 时释放。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    def __init__(
        self,
        dsn: str,
        *,
        echo: bool = False,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
        pool_recycle: int = 1800,
    ) -> None:
        self._engine: AsyncEngine = create_async_engine(
            dsn,
            echo=echo,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            # 连接闲置太久会被数据库或中间的防火墙悄悄掐掉，
            # 下次使用时才报"connection closed"。主动回收比事后重连干净。
            pool_recycle=pool_recycle,
            pool_pre_ping=True,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """一个工作单元。异常时回滚，正常退出**不自动提交** ——

        隐式提交会让"我只是读一下"的代码在出错时也写进去半截数据。
        需要写入的调用方显式 `await session.commit()`。
        """
        async with self._sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """一个事务。正常退出自动提交，异常回滚。"""
        async with self._sessionmaker() as session, session.begin():
            yield session

    async def aclose(self) -> None:
        await self._engine.dispose()


__all__ = ["Database"]
