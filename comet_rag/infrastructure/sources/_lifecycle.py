from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress


class _ActivityState:
    """清理独占生命周期；状态通知不占用文件工作线程。"""

    def _initialize_lifecycle(self) -> None:
        self._lifecycle = threading.Condition()
        self._active_loads = 0
        self._cleanup_in_progress = False
        self._waiters: set[asyncio.Future[None]] = set()

    def _end_activity(self) -> None:
        with self._lifecycle:
            self._active_loads -= 1
            if self._active_loads == 0:
                self._lifecycle.notify_all()
                self._notify_waiters_locked()

    def _end_cleanup(self) -> None:
        with self._lifecycle:
            self._cleanup_in_progress = False
            self._lifecycle.notify_all()
            self._notify_waiters_locked()

    def _new_waiter_locked(self) -> asyncio.Future[None]:
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        return waiter

    async def _wait_for_change(self, waiter: asyncio.Future[None]) -> None:
        try:
            await waiter
        finally:
            with self._lifecycle:
                self._waiters.discard(waiter)

    @staticmethod
    def _resolve_waiter(waiter: asyncio.Future[None]) -> None:
        if not waiter.done():
            waiter.set_result(None)

    def _notify_waiters_locked(self) -> None:
        waiters = tuple(self._waiters)
        self._waiters.clear()
        for waiter in waiters:
            with suppress(RuntimeError):
                waiter.get_loop().call_soon_threadsafe(
                    self._resolve_waiter,
                    waiter,
                )


class SyncActivity(_ActivityState):
    @contextmanager
    def _activity(self) -> Iterator[None]:
        self._begin_activity()
        try:
            yield
        finally:
            self._end_activity()

    def _begin_activity(self) -> None:
        with self._lifecycle:
            while self._cleanup_in_progress:
                self._lifecycle.wait()
            self._active_loads += 1

    def _begin_cleanup(self) -> None:
        with self._lifecycle:
            while self._cleanup_in_progress:
                self._lifecycle.wait()
            self._cleanup_in_progress = True
            while self._active_loads:
                self._lifecycle.wait()


class AsyncActivity(_ActivityState):
    @asynccontextmanager
    async def _activity(self) -> AsyncIterator[None]:
        await self._begin_activity()
        try:
            yield
        finally:
            self._end_activity()

    async def _begin_activity(self) -> None:
        """Register async activity without occupying a worker while cleanup runs."""

        while True:
            with self._lifecycle:
                if not self._cleanup_in_progress:
                    self._active_loads += 1
                    return
                waiter = self._new_waiter_locked()
            await self._wait_for_change(waiter)

    async def _begin_cleanup(self) -> None:
        owns_cleanup = False
        try:
            while not owns_cleanup:
                with self._lifecycle:
                    if not self._cleanup_in_progress:
                        self._cleanup_in_progress = True
                        owns_cleanup = True
                        break
                    waiter = self._new_waiter_locked()
                await self._wait_for_change(waiter)

            while True:
                with self._lifecycle:
                    if self._active_loads == 0:
                        return
                    waiter = self._new_waiter_locked()
                await self._wait_for_change(waiter)
        except BaseException:
            if owns_cleanup:
                self._end_cleanup()
            raise
