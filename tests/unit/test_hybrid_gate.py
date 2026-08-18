"""混合闸门：一份预算，两个入口。

这是所有并发的地基，所以用例盯的不是"能跑通"，而是**名额永远不会漏**：
漏一个，闸门就永久窄一格；漏够了就是全线阻塞，且监控上只表现为"越来越慢"。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
import time

import pytest

from comet_rag.core.concurrency import (
    Gate,
    Overloaded,
    _AsyncWaiter,
    _SyncWaiter,
)


def _available(gate: Gate) -> int:
    """当前还能拿几个名额 —— 用它检测泄漏。"""
    stats = gate.stats
    return stats.limit - stats.in_flight


# ── 一份预算 ───────────────────────────────────────────────────────────────


async def test_sync_and_async_share_one_budget() -> None:
    """**这是整个改动的目的。** 两侧各配一个信号量等于没限。"""
    gate = Gate(limit=4)
    live = 0
    peak = 0
    lock = threading.Lock()

    def touch() -> None:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)

    def untouch() -> None:
        nonlocal live
        with lock:
            live -= 1

    def sync_worker() -> None:
        for _ in range(8):
            with gate:
                touch()
                time.sleep(0.005)
                untouch()

    async def async_worker() -> None:
        for _ in range(8):
            async with gate:
                touch()
                await asyncio.sleep(0.005)
                untouch()

    threads = [threading.Thread(target=sync_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    await asyncio.gather(*[async_worker() for _ in range(8)])
    for thread in threads:
        thread.join()

    assert peak <= 4, f"上限 4，同步+异步合起来的真实峰值 {peak}"
    assert peak > 1, "峰值恒为 1 说明根本没并行"
    assert _available(gate) == 4, "结束后名额没有全部归还"


async def test_async_waiter_does_not_block_the_event_loop() -> None:
    """排队不能占着事件循环 —— 否则闸门一满，整个进程停摆。"""
    gate = Gate(limit=1)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.002)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)

    holder = threading.Thread(target=lambda: _hold_sync(gate, 0.15))
    holder.start()
    await asyncio.sleep(0.01)  # 确保名额已被同步侧占住
    async with gate:  # 必然要排队
        pass
    holder.join()
    beat.cancel()

    assert ticks >= 10, f"排队期间事件循环只调度了 {ticks} 次 —— 被堵住了"


def _hold_sync(gate: Gate, seconds: float) -> None:
    with gate:
        time.sleep(seconds)


# ── 名额不许漏 ─────────────────────────────────────────────────────────────


async def test_cancelling_a_waiting_coroutine_does_not_leak_a_permit() -> None:
    """**取消一个正在排队的调用，不能吃掉一个名额。**

    这是"每个等待者占一个线程"那种实现最容易踩的坑：协程被取消了，线程还在
    等，等到了就是一个再也回不来的名额。每取消一次漏一个，闸门越收越紧，
    最终全线阻塞 —— 而现象只是"服务越来越慢"。
    """
    gate = Gate(limit=1)

    async with gate:  # 占满
        waiters = [asyncio.create_task(_acquire_and_release(gate)) for _ in range(5)]
        await asyncio.sleep(0.02)
        assert gate.stats.waiting == 5
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        assert gate.stats.waiting == 0

    assert _available(gate) == 1, "取消排队者之后名额没有回来"

    # 还能正常用：漏名额的话这里会挂住
    async with asyncio.timeout(2), gate:
        pass


async def _acquire_and_release(gate: Gate) -> None:
    async with gate:
        await asyncio.sleep(0)


async def test_cancellation_racing_with_a_grant_does_not_leak() -> None:
    """取消与"名额刚好移交给它"撞上时，名额仍要回到池子里。

    这是最难的一条：`release()` 已经把名额记在等待者头上、并安排了唤醒，
    而调用方在同一瞬间放弃了。两条路径靠同一把锁分出胜负，输的那边负责归还。
    """
    for _ in range(50):  # 反复撞，撞出时序
        gate = Gate(limit=1)
        holder = await _enter(gate)
        waiter = asyncio.create_task(_acquire_and_release(gate))
        await asyncio.sleep(0)

        gate.release()  # 移交给 waiter
        waiter.cancel()  # 同一轮事件循环里放弃
        await asyncio.gather(waiter, return_exceptions=True)
        del holder

        assert _available(gate) == 1, "取消与移交撞车后漏了名额"


async def _enter(gate: Gate) -> object:
    await gate.acquire()
    return object()


def test_sync_timeout_does_not_leak_a_permit() -> None:
    """同步侧超时同样不能漏 —— 包括"超时之后才被移交"这种边界。"""
    gate = Gate(limit=1, acquire_timeout=0.02)

    with gate, pytest.raises(Overloaded), gate:  # 拿不到，超时
        pass

    assert _available(gate) == 1


async def test_async_timeout_does_not_leak_a_permit() -> None:
    gate = Gate(limit=1, acquire_timeout=0.02)

    async with gate:
        with pytest.raises(Overloaded):
            async with gate:
                pass

    assert _available(gate) == 1


# ── 背压仍然有界 ───────────────────────────────────────────────────────────


async def test_waiting_seats_are_still_bounded() -> None:
    gate = Gate(limit=1, max_waiting=2)

    async with gate:
        queued = [asyncio.create_task(_acquire_and_release(gate)) for _ in range(2)]
        await asyncio.sleep(0.02)

        with pytest.raises(Overloaded, match="等待席位已满"):
            async with gate:
                pass

        for task in queued:
            task.cancel()
        await asyncio.gather(*queued, return_exceptions=True)

    assert gate.stats.rejected >= 1


def test_sync_waiting_seats_share_the_same_queue() -> None:
    """同步等待者也占席位 —— 两个队列各算各的，`max_waiting` 就是假的。"""
    gate = Gate(limit=1, max_waiting=1, acquire_timeout=0.5)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with gate:
            entered.set()
            release.wait(1.0)

    def queued() -> None:
        with gate:
            pass

    threads = [threading.Thread(target=holder), threading.Thread(target=queued)]
    for thread in threads:
        thread.start()
    entered.wait(1.0)
    time.sleep(0.05)  # 让第二个线程排进队列

    with pytest.raises(Overloaded, match="等待席位已满"), gate:  # 第三个：席位已满
        pass

    release.set()
    for thread in threads:
        thread.join()
    assert _available(gate) == 1


# ── 移交而不是"放回池子" ───────────────────────────────────────────────────


async def test_release_transfers_the_permit_instead_of_pooling_it() -> None:
    """**名额直接移交给排最前面的人，不是放回池子让大家再抢。**

    放回池子的话，新来的调用走快路径（`available > 0` 立刻拿走），永远比排队
    的先到手 —— 排队者饿死，而现象只是"p99 莫名其妙地难看"。

    只看唤醒顺序是测不出区别的：两种实现都按 FIFO 唤醒。要看的是**释放那一
    瞬间名额去了哪**：移交的话它当场记在等待者头上（in_flight 不降回 0），
    放回池子的话它是空闲的，谁先来谁拿。
    """
    gate = Gate(limit=1)
    await gate.acquire()
    queued = [asyncio.create_task(_acquire_and_release(gate)) for _ in range(2)]
    await asyncio.sleep(0.02)
    assert gate.stats.waiting == 2

    gate.release()

    # 还没回到事件循环，等待者一行代码都没跑 —— 此刻的账本就是判据
    stats = gate.stats
    assert stats.in_flight == 1, (
        f"名额被放回池子了（in_flight={stats.in_flight}）：新来的调用会插队"
    )
    assert stats.waiting == 1, f"移交后队列该少一个，实际 waiting={stats.waiting}"

    await asyncio.gather(*queued)
    assert _available(gate) == 1


async def test_a_newcomer_cannot_jump_the_queue() -> None:
    """上一条的行为后果：释放的名额已经属于排队者，新来的只能排到后面。"""
    gate = Gate(limit=1)
    order: list[str] = []

    async def contender(tag: str) -> None:
        async with gate:
            order.append(tag)
            await asyncio.sleep(0)

    await gate.acquire()
    early = asyncio.create_task(contender("排队者"))
    await asyncio.sleep(0.02)

    gate.release()
    late = asyncio.create_task(contender("新来的"))
    await asyncio.gather(early, late)

    assert order == ["排队者", "新来的"], f"新来的插了队：{order}"


def test_observer_sees_the_sync_path_too() -> None:
    """降级判据的数据来源不能只覆盖异步侧 —— 否则同步流量在监控上是隐形的。"""
    seen: list[bool] = []
    gate = Gate(limit=2, observer=seen.append)

    with gate:
        pass
    with pytest.raises(RuntimeError), gate:
        raise RuntimeError("boom")

    assert seen == [True, False]


# ── 混合负载浸泡 ───────────────────────────────────────────────────────────


def _jitter(upper: float) -> float:
    """打散时序用的随机等待。用 `secrets` 只是为了不触发 ruff 的 S311 ——
    这里对随机性的要求仅仅是"别每次都一样"。"""
    return secrets.randbelow(1000) / 1000 * upper


async def test_mixed_load_never_leaks_a_permit() -> None:
    """**同步 + 异步 + 取消 + 超时一起上，名额一个都不能漏。**

    单条用例只能撞出它设计的那个时序。真正的竞态要靠量：这里让六个线程、
    六个协程和四个不停取消的协程抢同一个闸门约一秒，然后看账本是否归零。

    验收只有两条，但都是硬的：
      · 峰值从不超过上限 —— 否则限流是假的；
      · 结束后 in_flight 与 waiting 都是 0 —— 否则名额漏了，闸门会越收越紧。
    """
    gate = Gate(limit=4, max_waiting=32, acquire_timeout=0.03)
    stop = threading.Event()

    def sync_worker() -> None:
        while not stop.is_set():
            try:
                with gate:
                    time.sleep(_jitter(0.003))
            except Overloaded:
                pass

    async def async_worker() -> None:
        while not stop.is_set():
            try:
                async with gate:
                    await asyncio.sleep(_jitter(0.003))
            except Overloaded:
                pass

    async def canceller() -> None:
        while not stop.is_set():
            task = asyncio.create_task(async_worker())
            await asyncio.sleep(_jitter(0.005))
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    threads = [threading.Thread(target=sync_worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    tasks = [asyncio.create_task(async_worker()) for _ in range(6)]
    tasks += [asyncio.create_task(canceller()) for _ in range(4)]

    await asyncio.sleep(0.6)
    stop.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for thread in threads:
        thread.join(timeout=5)
    await asyncio.sleep(0.05)

    stats = gate.stats
    assert stats.peak_in_flight <= 4, f"峰值 {stats.peak_in_flight} 超过上限 4"
    assert stats.admitted > 100, f"只准入了 {stats.admitted} 次，压力不够说明不了问题"
    assert stats.in_flight == 0, f"结束后仍有 {stats.in_flight} 个名额在外"
    assert stats.waiting == 0, f"结束后仍有 {stats.waiting} 个等待者"


# ── 评审补的边界 ───────────────────────────────────────────────────────────


def test_cancellation_is_not_counted_as_a_rejection() -> None:
    """**用户取消不是过载。**

    `/admin/limits` 的文档写着"`rejected` 在涨说明已经在丢请求"。把取消混进去，
    运维会把"用户撤了几个任务"读成"服务在丢请求"；更糟的是计不计还取决于取消
    有没有跟移交撞上 —— 一个随时序抖动的指标比没有这个指标更糟。

    这与 `observer` 把 `CancelledError` 排除在失败率之外是同一条原则。
    """

    async def scenario() -> Gate:
        gate = Gate(limit=1)
        async with gate:
            queued = [asyncio.create_task(_acquire_and_release(gate)) for _ in range(3)]
            await asyncio.sleep(0.02)
            for task in queued:
                task.cancel()
            await asyncio.gather(*queued, return_exceptions=True)
        return gate

    gate = asyncio.run(scenario())
    assert gate.stats.rejected == 0, (
        f"取消被计成了 {gate.stats.rejected} 次拒绝"
    )


async def test_timeout_is_still_counted_as_a_rejection() -> None:
    """对照：超时确实是过载，必须计数 —— 否则上一条就是把指标关掉了。"""
    gate = Gate(limit=1, acquire_timeout=0.02)

    async with gate:
        with pytest.raises(Overloaded):
            async with gate:
                pass

    assert gate.stats.rejected == 1


def test_a_waiter_on_a_closed_loop_does_not_swallow_the_permit() -> None:
    """**唤醒失败不能吃掉名额。**

    异步等待者记着自己的事件循环。那个循环若已关闭，`call_soon_threadsafe`
    抛 `RuntimeError` —— 而此时名额已经从在途里减掉、又没交到任何人手上。
    异常一冒出去，闸门就永久少一格。

    醒不来的等待者跳过，名额让给下一个（这里是让回池子）。
    """
    gate = Gate(limit=1)
    dead_loop = asyncio.new_event_loop()

    async def occupy() -> None:
        await gate.acquire()

    live_loop = asyncio.new_event_loop()
    try:
        live_loop.run_until_complete(occupy())  # 名额被占满
        # 手工塞一个"属于已关闭循环"的等待者，模拟 worker 循环先行退出
        waiter = _AsyncWaiter(dead_loop)
        with gate._lock:  # noqa: SLF001
            gate._waiters.append(waiter)  # noqa: SLF001
        dead_loop.close()

        gate.release()  # 唤醒必然失败
    finally:
        live_loop.close()

    stats = gate.stats
    assert stats.in_flight == 0, f"名额被吞了：in_flight={stats.in_flight}"
    assert stats.waiting == 0

    # 还能正常用 —— 吞掉名额的话这里会挂住
    async def reuse() -> None:
        async with asyncio.timeout(2), gate:
            pass

    again = asyncio.new_event_loop()
    try:
        again.run_until_complete(reuse())
    finally:
        again.close()


def test_waiting_on_the_event_loop_thread_fails_fast() -> None:
    """**在事件循环线程上等同步名额 = 挂死整个进程，必须当场报错。**

    `threading.Event.wait()` 阻塞调用线程。若那是事件循环线程，唤醒回调永远
    跑不了、持有名额的协程也永远跑不到 `release()` —— 双方互等，而
    `acquire_timeout=None` 时没有逃生路径。

    抛 `RuntimeError` 而不是 `Overloaded`：这是**误用**不是过载，后者会被调用
    方当成"退避重试"，而重试多少次都一样。
    """

    async def scenario() -> None:
        gate = Gate(limit=1)
        async with gate:  # 名额被本循环上的协程占住
            with pytest.raises(RuntimeError, match="不能在事件循环线程上"):
                gate.acquire_sync()

    asyncio.run(scenario())


def test_fast_path_still_works_on_the_event_loop_thread() -> None:
    """只在**慢路径**拦：没争用时同步入口在协程里也能直接用。

    否则"同步 API 在异步上下文里一律报错"会误伤大量正常调用 —— 而它们根本
    不会阻塞。
    """

    async def scenario() -> None:
        gate = Gate(limit=2)
        with gate:  # 有空位，立刻拿到，不阻塞
            pass

    asyncio.run(scenario())


def test_fail_fast_does_not_leak_a_seat() -> None:
    """快速失败也要把自己从队列里摘掉，否则席位被永久占住。"""

    async def scenario() -> Gate:
        gate = Gate(limit=1, max_waiting=1)
        async with gate:
            with pytest.raises(RuntimeError):
                gate.acquire_sync()
        return gate

    gate = asyncio.run(scenario())
    assert gate.stats.waiting == 0
    assert gate.stats.rejected == 0, "误用不是过载，不该计入 rejected"
    assert gate.stats.limit - gate.stats.in_flight == 1


def test_timeout_is_counted_whether_or_not_it_races_with_a_grant() -> None:
    """**同一件事，计不计不能取决于时序。**

    超时被拒是过载的证据，无论它有没有刚好撞上"名额正被移交给我"。只在
    "还在队列里"那一支计数的话，`rejected` 会随调度抖动地少计 —— 而调用方
    两种情况下都拿到了 `Overloaded`。

    这与取消一律不计是同一条原则的两面：**判据是发生了什么，不是撞上了什么。**
    """
    still_queued = Gate(limit=1, acquire_timeout=0.01)
    waiter = _SyncWaiter()
    with still_queued._lock:  # noqa: SLF001
        still_queued._waiters.append(waiter)  # noqa: SLF001
    assert still_queued._abandon(waiter, rejected=True) is False  # noqa: SLF001

    raced = Gate(limit=1, acquire_timeout=0.01)
    granted = _SyncWaiter()  # 不在队列里 = 已被移交
    assert raced._abandon(granted, rejected=True) is True  # noqa: SLF001

    assert still_queued.stats.rejected == raced.stats.rejected == 1
