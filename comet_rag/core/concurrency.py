"""并发闸门与有界背压（spec S4-1、S4-2）。

## 为什么"每次调用建一个信号量"是错的

`BaseEmbeddingModel.abatch_embed` 原本每次调用都 `asyncio.Semaphore(max_concurrency)`。
那个信号量只约束**这一次调用内部**的扇出，任务之间毫无关系。实测：

    embedder worker 的 max_jobs=32，每个任务 abatch_embed(max_concurrency=4)
    → 对模型服务的并发峰值 **128**，而配置写的是 4

配置说 4、实际 128，且监控上完全看不出来 —— 每个任务都觉得自己很守规矩。
真正该被约束的是**进程对模型服务的总并发**，所以闸门必须是进程级的、
被所有调用方共用的一个对象。

## 有界，而不是无限排队

闸门满了之后，请求要么等、要么被拒。**不能无限等下去**：
  · 等待者本身占内存（协程 + 它持有的 chunk 文本），投递量一大就 OOM；
  · 等了五分钟才轮到自己的请求，上游早就超时了，算出来也没人要。

所以有两道界：`max_waiting`（等待席位数）与 `acquire_timeout`（最长等待时间）。
任一越界都抛 `Overloaded` —— **明确拒绝，不静默丢弃**（S4-1）。
调用方据此翻译成 HTTP 429 或可重试失败，客户端知道该退避重来。

## 闸门不管同步路径

`Gate` 基于 `asyncio.Semaphore`，只能约束事件循环内的协程。同步的
`embed()` / `score()` 走线程池，不经过它 —— 那条路是给"当库用"的场景准备的，
服务端全链路异步，不会走到。刻意不用 `threading.Semaphore` 再套一层：
两套计数各管各的，合起来的上限反而说不清楚。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from comet_rag.core.logging import logger


class Overloaded(RuntimeError):
    """闸门已满且等待席位/时间也耗尽。**明确的拒绝**，不是内部错误。

    调用方应翻译成：API 层 HTTP 429、任务层可重试失败。
    两者都会让压力自然回落，而不是把请求堆在内存里。
    """

    def __init__(self, gate: str, detail: str) -> None:
        super().__init__(f"{gate} 闸门过载：{detail}")
        self.gate = gate


@dataclass(slots=True)
class GateStats:
    """闸门的运行时观测值。`peak_in_flight` 是验收 S4-2 的那个数。"""

    limit: int
    in_flight: int
    waiting: int
    peak_in_flight: int
    peak_waiting: int
    admitted: int
    rejected: int


class Gate:
    """进程级并发闸门。`async with gate:` 包住一次下游调用。

    `limit` 是同时在途的上限；`max_waiting` 是闸门外允许排队的数量
    （0 表示不限，但那就失去了背压意义，仅供测试与"当库用"时放行）。
    """

    def __init__(
        self,
        *,
        limit: int,
        max_waiting: int = 0,
        acquire_timeout: float | None = None,
        name: str = "model",
        observer: Callable[[bool], None] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError(f"闸门上限必须为正，收到 {limit}")
        self.name = name
        #: 每次调用的成败都报给它（降级判据的数据来源）。观测点放在闸门上，
        #: 是因为所有对模型服务的调用都必然穿过这里 —— 与"闸门绕不过去"
        #: 同一个理由，不需要在每个调用点插桩，也就不会有人漏插。
        self._observer = observer
        self._limit = limit
        self._max_waiting = max_waiting
        self._timeout = acquire_timeout
        self._sem = asyncio.Semaphore(limit)
        self._in_flight = 0
        self._waiting = 0
        self._peak_in_flight = 0
        self._peak_waiting = 0
        self._admitted = 0
        self._rejected = 0

    # ── 观测 ───────────────────────────────────────────────────────────────

    @property
    def stats(self) -> GateStats:
        return GateStats(
            limit=self._limit,
            in_flight=self._in_flight,
            waiting=self._waiting,
            peak_in_flight=self._peak_in_flight,
            peak_waiting=self._peak_waiting,
            admitted=self._admitted,
            rejected=self._rejected,
        )

    def reset_peaks(self) -> None:
        """清峰值。给 benchmark 与测试用，生产不该调。"""
        self._peak_in_flight = self._in_flight
        self._peak_waiting = self._waiting

    # ── 获取与释放 ─────────────────────────────────────────────────────────

    async def acquire(self) -> None:
        if self._max_waiting and self._waiting >= self._max_waiting:
            self._rejected += 1
            raise Overloaded(
                self.name,
                f"等待席位已满（{self._waiting}/{self._max_waiting}），"
                f"在途 {self._in_flight}/{self._limit}",
            )

        self._waiting += 1
        self._peak_waiting = max(self._peak_waiting, self._waiting)
        try:
            if self._timeout is None:
                await self._sem.acquire()
            else:
                # ⚠️ 超时后 `Semaphore.acquire` 必须干净地把名额还回去，否则
                # 每次超时都漏掉一个名额，闸门会随时间越收越紧、最终全线阻塞。
                # CPython 3.12 的实现已正确处理取消，`test_gate.py` 里有一条
                # 用例专门盯着这件事 —— 换 Python 版本时它会替我们把关。
                await asyncio.wait_for(self._sem.acquire(), self._timeout)
        except TimeoutError:
            self._rejected += 1
            raise Overloaded(
                self.name,
                f"等待超过 {self._timeout:g}s（在途 {self._in_flight}/{self._limit}）",
            ) from None
        finally:
            self._waiting -= 1

        self._in_flight += 1
        self._admitted += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

    def release(self) -> None:
        self._in_flight -= 1
        self._sem.release()

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
        # 取消不算失败：那是调用方主动放弃，不是下游出了问题。
        # 把它算进失败率会让"用户取消了几个任务"看起来像服务故障。
        if self._observer is not None and exc_type is not asyncio.CancelledError:
            self._observer(exc_type is None)


def build_gate(
    *,
    limit: int,
    max_waiting: int,
    acquire_timeout: float | None,
    name: str = "model",
    observer: Callable[[bool], None] | None = None,
) -> Gate:
    gate = Gate(
        limit=limit,
        max_waiting=max_waiting,
        acquire_timeout=acquire_timeout,
        name=name,
        observer=observer,
    )
    logger.info(
        f"并发闸门就绪 name={name} limit={limit} "
        f"max_waiting={max_waiting or '不限'} timeout={acquire_timeout or '不限'}"
    )
    return gate


__all__ = ["Gate", "GateStats", "Overloaded", "build_gate"]
