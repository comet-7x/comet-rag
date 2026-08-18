"""并发闸门与有界背压（spec S4-1、S4-2）。

## 为什么"每次调用建一个信号量"是错的

批量嵌入的排程（今在 `engines/embedding/batch.py`）每次调用都新建一个
`asyncio.Semaphore(max_concurrency)`。那个信号量只约束**这一次调用内部**的
扇出，任务之间毫无关系。实测：

    embedder worker 的 max_jobs=32，每个任务 max_concurrency=4
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

## 同步路径也走同一份预算

这里原先写着"闸门不管同步路径"，理由是"两套计数各管各的，合起来的上限反而
说不清楚"。**那个理由是对的，结论是错的** —— 正确的做法不是放弃同步侧，
而是让两侧共用**同一个**计数。

放弃同步侧的代价实测出来了（#44）：`Pipeline.batch_run` 用 `max_concurrency`
个线程跑来源，每个来源内部再开 `max_concurrency` 路，而同步 `embed()` 不经过
闸门 —— 配置写 4，真实并发 16。加载侧同理。

所以预算换成了 `threading` 原语：它**两侧都能用**，而 `asyncio.Semaphore`
只有协程侧能用，同步路径必然漏出去。判据很简单 —— 能不能在线程里拿。

## 为什么不是"每个等待者占一个线程"

最直接的写法是异步侧用 `asyncio.to_thread` 去阻塞等待。它能跑通，但有两个
真问题：

  · `max_waiting=256` 时就是 256 个阻塞线程，还会挤占 `to_thread` 的公共池，
    拖慢与闸门无关的调用；
  · **协程被取消时线程还在等**，等到了就是一个漏掉的名额 —— 每取消一次漏一个，
    闸门随时间越收越紧，最终全线阻塞。

所以这里手写了双队列：一个计数、一条 FIFO 等待队列，队列里同时装同步等待者
（`threading.Event`）与异步等待者（`asyncio.Future`）。等待不占线程，取消
只是把自己从队列里摘掉。
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
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


class _Waiter:
    """排在闸门外的一个等待者。同步与异步只差"怎么被叫醒"。

    `granted` 由 `release()` 在持锁时设置，并把名额**直接移交**给它 ——
    而不是"放回池子让它自己再抢"。后者会让等待者饿死：新来的调用走快路径，
    永远比排队的先拿到。
    """

    __slots__ = ("granted",)

    def __init__(self) -> None:
        self.granted = False

    def wake(self) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError


class _SyncWaiter(_Waiter):
    __slots__ = ("event",)

    def __init__(self) -> None:
        super().__init__()
        self.event = threading.Event()

    def wake(self) -> None:
        self.event.set()


class _AsyncWaiter(_Waiter):
    __slots__ = ("_future", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = loop
        self._future: asyncio.Future[None] = loop.create_future()

    @property
    def future(self) -> asyncio.Future[None]:
        return self._future

    def wake(self) -> None:
        # 可能从**别的线程**调用（同步侧 release 唤醒异步等待者），
        # 所以必须 call_soon_threadsafe。
        self._loop.call_soon_threadsafe(self._resolve)

    def _resolve(self) -> None:
        if not self._future.done():
            self._future.set_result(None)


class Gate:
    """进程级并发闸门。`async with gate:` 或 `with gate:` 包住一次下游调用。

    `limit` 是同时在途的上限；`max_waiting` 是闸门外允许排队的数量
    （0 表示不限，但那就失去了背压意义，仅供测试与"当库用"时放行）。

    **同步与异步共用同一份预算。** 两个入口，一个计数 —— 见模块文档。
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
        #: 是因为所有对下游的调用都必然穿过这里 —— 与"闸门绕不过去"同一个
        #: 理由，不需要在每个调用点插桩，也就不会有人漏插。
        self._observer = observer
        self._limit = limit
        self._max_waiting = max_waiting
        self._timeout = acquire_timeout
        #: 一把锁护住下面**所有**状态。临界区里只做记账，不做任何阻塞操作。
        self._lock = threading.Lock()
        self._available = limit
        self._waiters: deque[_Waiter] = deque()
        self._in_flight = 0
        self._peak_in_flight = 0
        self._peak_waiting = 0
        self._admitted = 0
        self._rejected = 0

    # ── 观测 ───────────────────────────────────────────────────────────────

    @property
    def stats(self) -> GateStats:
        with self._lock:
            return GateStats(
                limit=self._limit,
                in_flight=self._in_flight,
                waiting=len(self._waiters),
                peak_in_flight=self._peak_in_flight,
                peak_waiting=self._peak_waiting,
                admitted=self._admitted,
                rejected=self._rejected,
            )

    def reset_peaks(self) -> None:
        """清峰值。给 benchmark 与测试用，生产不该调。"""
        with self._lock:
            self._peak_in_flight = self._in_flight
            self._peak_waiting = len(self._waiters)

    # ── 记账（全部在锁内调用）───────────────────────────────────────────────

    def _enter_locked(self) -> None:
        self._in_flight += 1
        self._admitted += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

    def _overloaded_waiting(self) -> Overloaded:
        return Overloaded(
            self.name,
            f"等待席位已满（{len(self._waiters)}/{self._max_waiting}），"
            f"在途 {self._in_flight}/{self._limit}",
        )

    def _overloaded_timeout(self) -> Overloaded:
        timeout = self._timeout or 0.0
        return Overloaded(
            self.name,
            f"等待超过 {timeout:g}s（在途 {self._in_flight}/{self._limit}）",
        )

    def _try_take[W: _Waiter](self, make_waiter: Callable[[], W]) -> W | None:
        """立刻拿到就返回 None；需要排队则登记并返回等待者。"""
        with self._lock:
            if self._available > 0:
                self._available -= 1
                self._enter_locked()
                return None
            if self._max_waiting and len(self._waiters) >= self._max_waiting:
                self._rejected += 1
                raise self._overloaded_waiting()
            waiter = make_waiter()
            self._waiters.append(waiter)
            self._peak_waiting = max(self._peak_waiting, len(self._waiters))
            return waiter

    def _abandon(self, waiter: _Waiter, *, rejected: bool) -> bool:
        """放弃等待。返回"是否已经被授予名额"。

        两种结局，靠"还在不在队列里"区分，而这个判断在锁内做，所以与
        `release()` 的移交是互斥的 —— 不会出现两边都以为自己持有名额。

        `rejected` 只在**超时**时为真。取消不算过载：那是调用方主动放弃，
        与下游是否扛不住无关。混进去会让 `/admin/limits` 上的 `rejected`
        （文档写着"在涨说明已经在丢请求"）把用户取消的任务报成丢弃的请求，
        而且计不计还取决于取消有没有跟移交撞上 —— 一个随时序抖动的指标比
        没有这个指标更糟（评审指出）。这与 `_report()` 把 CancelledError
        排除在失败率之外是同一条原则。
        """
        with self._lock:
            if rejected:
                # **两个分支都要计。** 只在"还在队列里"那支计数的话，超时若刚好
                # 撞上移交就不计 —— 于是同一件事（请求因过载被拒）计不计取决于
                # 时序。那正是本方法上面那段话反对的东西，我先前只把它用在了
                # 取消上（评审指出）。
                self._rejected += 1
            try:
                self._waiters.remove(waiter)
            except ValueError:
                return True  # 已被移交：名额在我们手上，调用方要负责还回去
            return False

    # ── 获取与释放 ─────────────────────────────────────────────────────────

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        waiter = self._try_take(lambda: _AsyncWaiter(loop))
        if waiter is None:
            return
        try:
            if self._timeout is None:
                await waiter.future
            else:
                await asyncio.wait_for(waiter.future, self._timeout)
        except TimeoutError:
            self._give_back(waiter, rejected=True)
            raise self._overloaded_timeout() from None
        except asyncio.CancelledError:
            # **取消也必须清理。** 漏掉这一步，闸门会随取消次数越收越紧：
            # 每取消一个正在排队的调用，就永久少掉一个名额，最终全线阻塞。
            # 取消原样抛出去，不翻译成 Overloaded —— 那是调用方自己的决定，
            # 不是过载。
            self._give_back(waiter, rejected=False)
            raise

    def _give_back(self, waiter: _Waiter, *, rejected: bool) -> None:
        """放弃等待后的收尾：名额若已移交到手上，必须还回去。"""
        if self._abandon(waiter, rejected=rejected):
            self.release()

    def acquire_sync(self) -> None:
        waiter = self._try_take(_SyncWaiter)
        if waiter is None:
            return  # 快路径：没争用，不阻塞，也就无所谓在哪个线程
        self._refuse_to_block_the_loop(waiter)
        if waiter.event.wait(self._timeout):
            return
        if self._abandon(waiter, rejected=True):
            # 超时之后才被移交：名额确实在手上，但调用方已经不要了，还回去。
            self.release()
        raise self._overloaded_timeout()

    def _refuse_to_block_the_loop(self, waiter: _SyncWaiter) -> None:
        """在事件循环线程上排队 = 把整个循环挂住，**必须当场报错**。

        `threading.Event.wait()` 阻塞调用线程。若那是事件循环线程：

          · `_AsyncWaiter.wake()` 的 `call_soon_threadsafe` 回调永远跑不了；
          · 持有名额的协程也永远跑不到 `release()`。

        于是双方互等，而 `acquire_timeout=None` 时**没有逃生路径** —— 现象是
        整个进程静默卡死（评审指出）。

        只在**慢路径**检查：快路径没有阻塞，也就没有这个风险，不必为它付
        `get_running_loop()` 的开销。这个错误是**误用**而不是过载，所以抛
        `RuntimeError` 而不是 `Overloaded` —— 后者会被调用方当成"退避重试"，
        而重试多少次都一样。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有运行中的循环：这是普通工作线程，阻塞是安全的
        self._abandon(waiter, rejected=False)
        raise RuntimeError(
            f"{self.name} 闸门：不能在事件循环线程上等待同步名额。"
            f"协程里请用 `async with gate:`（或模型/加载器的 a* 入口）；"
            f"确需同步 API 时把它放进 `asyncio.to_thread`。"
        )

    def release(self) -> None:
        """归还一个名额：优先**直接移交**给排在最前面的等待者。

        唤醒可能失败：异步等待者记着自己的事件循环，那个循环若已关闭，
        `call_soon_threadsafe` 会抛 `RuntimeError`。此时不能让异常冒出去 ——
        名额已经从在途里减掉、又没交到任何人手上，就这么丢了一格
        （评审指出）。醒不来的等待者直接跳过，名额让给下一个。
        """
        with self._lock:
            self._in_flight -= 1
            while self._waiters:
                waiter = self._waiters.popleft()
                try:
                    waiter.wake()
                except RuntimeError:
                    continue  # 循环已关闭，这个等待者永远醒不来
                waiter.granted = True
                self._enter_locked()
                return
            self._available += 1

    # ── 上下文管理器 ───────────────────────────────────────────────────────

    def _report(self, exc_type: type[BaseException] | None) -> None:
        # 取消不算失败：那是调用方主动放弃，不是下游出了问题。
        # 把它算进失败率会让"用户取消了几个任务"看起来像服务故障。
        if self._observer is not None and exc_type is not asyncio.CancelledError:
            self._observer(exc_type is None)

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
        self._report(exc_type)

    def __enter__(self) -> Self:
        self.acquire_sync()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
        self._report(exc_type)


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
