"""并发闸门与有界背压（spec S4-1、S4-2）。

本文件盯着一个**实测过的真实缺陷**：闸门原本是"每次调用建一个信号量"，
于是 32 个任务各开 4 路扇出时，对模型服务的并发峰值是 **128**，而配置写的
是 4。配置说 4、实际 128，且监控上完全看不出来 —— 每个任务都觉得自己很守规矩。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from comet_rag.core.concurrency import Gate, Overloaded
from comet_rag.engines.embedding import async_batch, batch
from comet_rag.infrastructure.models.embedding.base import (
    AsyncBaseEmbeddingModel,
    AsyncMultimodalEmbeddingMixin,
    BaseEmbeddingModel,
    MultimodalEmbeddingMixin,
)
from comet_rag.infrastructure.models.reranker.base import (
    AsyncBaseReranker,
    BaseReranker,
)
from comet_rag.ports import MediaResource


class RecordingEmbedding(MultimodalEmbeddingMixin, BaseEmbeddingModel):
    """同步替身记录真实在途数；线程间用锁保护计数。"""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.live = 0
        self.peak = 0
        self._sync_lock = threading.Lock()

    def _embed(self, data: Any, **kwargs: Any) -> list[float]:
        with self._sync_lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(self.delay)
            return [0.0]
        finally:
            with self._sync_lock:
                self.live -= 1

    def _embed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        return self._embed("<media>", **kwargs)


class AsyncRecordingEmbedding(AsyncMultimodalEmbeddingMixin, AsyncBaseEmbeddingModel):
    """异步替身有独立生命周期；不继承同步客户端或同步执行方法。"""

    def __init__(self, overlap: int = 4) -> None:
        # 先证明指定数量的请求同时在途，再释放这一轮；串行退化会明确超时。
        self._overlap = asyncio.Barrier(overlap)
        self.live = 0
        self.peak = 0
        self.calls = 0

    async def _embed(self, data: Any, **kwargs: Any) -> list[float]:
        self.calls += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            async with asyncio.timeout(2):
                await self._overlap.wait()
                await asyncio.sleep(0)  # 单席位也保留真实排队/过载的竞争窗口。
        finally:
            self.live -= 1
        return [0.0]

    async def _embed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        return await self._embed("<media>", **kwargs)


class RecordingReranker(BaseReranker[str]):
    def __init__(self, sink: RecordingEmbedding) -> None:
        self.sink = sink

    def _score(self, query: str, documents: Any, **kwargs: Any) -> list[float]:
        self.sink._embed(query)
        return [0.0] * len(documents)


class AsyncRecordingReranker(AsyncBaseReranker[str]):
    def __init__(self, sink: AsyncRecordingEmbedding) -> None:
        self.sink = sink

    async def _score(self, query: str, documents: Any, **kwargs: Any) -> list[float]:
        await self.sink._embed(query)
        return [0.0] * len(documents)


# ── Gate 本身 ──────────────────────────────────────────────────────────────


async def test_gate_caps_concurrency() -> None:
    gate = Gate(limit=3)
    live = peak = 0
    overlap = asyncio.Barrier(3)

    async def work() -> None:
        nonlocal live, peak
        async with gate:
            live += 1
            peak = max(peak, live)
            async with asyncio.timeout(2):
                await overlap.wait()
            live -= 1

    async with asyncio.TaskGroup() as requests:
        for _ in range(30):
            requests.create_task(work())

    assert peak == 3, f"并发峰值 {peak} ≠ 上限 3"
    assert gate.stats.in_flight == 0, "全部结束后还有在途，说明有名额没还回来"


async def test_gate_rejects_when_the_waiting_room_is_full() -> None:
    """有界背压：等待席位满了就**明确拒绝**，不是无限排队。

    无限排队等于把 OOM 往后推 —— 等待者本身占着内存。
    """
    gate = Gate(limit=1, max_waiting=2)
    holding = asyncio.Event()

    async def hog() -> None:
        async with gate:
            await holding.wait()

    hogger = asyncio.create_task(hog())
    await asyncio.sleep(0)
    waiters = [asyncio.create_task(gate.acquire()) for _ in range(2)]
    await asyncio.sleep(0)  # 让两个等待者真的排进去

    with pytest.raises(Overloaded, match="等待席位已满"):
        await gate.acquire()

    holding.set()
    await hogger
    for w in waiters:
        await w
        gate.release()
    assert gate.stats.rejected == 1


async def test_gate_rejects_when_waiting_too_long() -> None:
    """等太久也算过载：上游多半早就超时了，算出来也没人要。"""
    gate = Gate(limit=1, acquire_timeout=0.05)
    released = asyncio.Event()

    async def hog() -> None:
        async with gate:
            await released.wait()

    hogger = asyncio.create_task(hog())
    await asyncio.sleep(0)

    with pytest.raises(Overloaded, match="等待超过"):
        await gate.acquire()

    released.set()
    await hogger


async def test_timed_out_waiters_do_not_leak_permits() -> None:
    """**超时不得漏名额**，否则闸门会随时间越收越紧，最后全线阻塞。

    `asyncio.wait_for` 取消的是 `Semaphore.acquire`；若那次取消发生在名额
    已被授予、协程尚未恢复的窗口里，实现必须把名额还回去。CPython 3.12 做
    对了这件事 —— 本用例是换 Python 版本时的看门人，症状（服务跑几小时后
    莫名越来越慢）非常难反查到这里。
    """
    gate = Gate(limit=2, acquire_timeout=0.02)
    released = asyncio.Event()

    async def hog() -> None:
        async with gate:
            await released.wait()

    hogs = [asyncio.create_task(hog()) for _ in range(2)]
    await asyncio.sleep(0.01)

    for _ in range(5):  # 多次超时足以暴露累计泄漏
        with pytest.raises(Overloaded):
            await gate.acquire()

    released.set()
    await asyncio.gather(*hogs)

    # 名额若漏了，这里就拿不满 2 个
    await asyncio.wait_for(asyncio.gather(gate.acquire(), gate.acquire()), timeout=1.0)
    assert gate.stats.in_flight == 2
    gate.release()
    gate.release()


async def test_gate_releases_on_exception() -> None:
    gate = Gate(limit=1)
    with pytest.raises(RuntimeError):
        async with gate:
            raise RuntimeError("boom")
    assert gate.stats.in_flight == 0
    async with gate:  # 还拿得到就说明没泄漏
        pass


# ── 模型层：闸门必须绕不过去 ────────────────────────────────────────────────


async def test_per_call_fanout_alone_does_not_cap_the_service() -> None:
    """**这就是那个被实测出来的缺陷。**

    每次调用各建一个信号量时，上限会随任务数翻倍：
    多个任务各自守住 4 路，合计仍会突破配置上限。
    """
    model = AsyncRecordingEmbedding()  # 不挂闸门 = 修复前的行为

    async with asyncio.TaskGroup() as requests:
        for _ in range(8):
            requests.create_task(
                async_batch.embed_documents(model, ["x"] * 4, max_concurrency=4)
            )

    assert model.peak > 4, (
        "没挂闸门却把并发压在了 4 以内 —— 那这条用例已经证明不了什么了"
    )


async def test_process_gate_caps_the_service_no_matter_how_many_tasks() -> None:
    """挂上进程级闸门后，任务数再多也不会突破上限（S4-2）。"""
    model = AsyncRecordingEmbedding()
    model.bind_gate(Gate(limit=4))

    async with asyncio.TaskGroup() as requests:
        for _ in range(8):
            requests.create_task(
                async_batch.embed_documents(model, ["x"] * 4, max_concurrency=4)
            )

    assert model.peak <= 4, f"并发峰值 {model.peak} 超过闸门上限 4"
    assert model.peak > 1, "并发度恒为 1 说明根本没并行，闸门测了个寂寞"
    assert model.calls == 8 * 4, "闸门不该吞掉任何一次调用"


async def test_embedding_and_reranker_share_one_gate() -> None:
    """两者抢同一块 GPU，各配一个闸门等于没限：两边都配 4，服务端看到 8。"""
    model = AsyncRecordingEmbedding()
    reranker = AsyncRecordingReranker(model)
    gate = Gate(limit=4)
    model.bind_gate(gate)
    reranker.bind_gate(gate)

    async with asyncio.TaskGroup() as requests:
        requests.create_task(
            async_batch.embed_documents(model, ["x"] * 20, max_concurrency=20)
        )
        for _ in range(20):
            requests.create_task(reranker.score("q", ["d"]))

    assert model.peak <= 4, f"两条路合起来的并发峰值 {model.peak} 超过 4"


def test_gate_is_not_bypassable_by_subclasses() -> None:
    """两套模板均把公开入口设为 final，适配器只实现底层请求。"""
    from comet_rag.infrastructure.models.embedding.openai import (
        AsyncOpenAIEmbeddingModel,
        OpenAIEmbeddingModel,
    )
    from comet_rag.infrastructure.models.embedding.qwen3_vl import (
        AsyncQwen3VLEmbeddingModel,
        Qwen3VLEmbeddingModel,
    )
    from comet_rag.infrastructure.models.reranker.qwen3_vl import (
        AsyncQwen3VLReranker,
        Qwen3VLReranker,
    )

    for base in (BaseEmbeddingModel, AsyncBaseEmbeddingModel):
        assert getattr(base._embed, "__isabstractmethod__", False)
        assert getattr(base.embed, "__final__", False)
    for base in (BaseReranker, AsyncBaseReranker):
        assert getattr(base._score, "__isabstractmethod__", False)
        assert getattr(base.score, "__final__", False)
    for cls in (
        Qwen3VLEmbeddingModel,
        AsyncQwen3VLEmbeddingModel,
        OpenAIEmbeddingModel,
        AsyncOpenAIEmbeddingModel,
    ):
        assert "embed" not in vars(cls), f"{cls.__name__} 覆写了 embed，绕过了闸门"
    for cls in (Qwen3VLReranker, AsyncQwen3VLReranker):
        assert "score" not in vars(cls), f"{cls.__name__} 覆写了 score，绕过了闸门"


async def test_overload_propagates_instead_of_being_swallowed() -> None:
    """过载必须以异常形式传出去 —— 静默丢弃比报错糟得多（S4-1）。"""
    model = AsyncRecordingEmbedding(overlap=1)
    model.bind_gate(Gate(limit=1, max_waiting=1))

    results = await asyncio.gather(
        *(model.embed("x") for _ in range(6)), return_exceptions=True
    )

    rejected = [r for r in results if isinstance(r, Overloaded)]
    assert rejected, "闸门爆了却一个错都没报，调用方无从得知"
    assert (
        len(rejected) + len([r for r in results if not isinstance(r, BaseException)])
        == 6
    )


async def test_media_entry_is_gated_like_text() -> None:
    """**图片入口也必须走闸门。**

    图片请求通常比文本更重（一张图能顶几十倍 token），如果异步 `embed_media`
    绕开闸门，限流就等于开了个后门：配了 4 并发，实际可以有 4 + N 个在飞。

    这条用例把文本与多模态混在一起打，只看**真实并发峰值**。
    """
    model = AsyncRecordingEmbedding(overlap=2)
    gate = Gate(limit=2, max_waiting=64)
    model.bind_gate(gate)

    media = MediaResource(url="https://example.invalid/a.png")
    async with asyncio.TaskGroup() as requests:
        for i in range(6):
            requests.create_task(model.embed(f"text-{i}"))
            requests.create_task(model.embed_media(media))

    assert model.calls == 12
    assert model.peak <= 2, f"闸门限 2，实际峰值 {model.peak} —— 多模态绕过了闸门"


# ── 同步路径与异步路径共用同一份预算（#44 已修）─────────────────────────────


class _SyncCounting(BaseEmbeddingModel):
    """只数同步 `embed` 的真实并发峰值。"""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0

    def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(self.delay)
        with self._lock:
            self.live -= 1
        return [0.0]


def test_sync_fanout_respects_the_process_gate() -> None:
    """**同步扇出必须与异步共用同一份预算。**

    `Pipeline.batch_run` 用 `max_concurrency` 个线程跑来源，每个来源内部的
    `embed_documents` 又开 `max_concurrency` 路。修复前同步 `embed` 完全不经过
    闸门（那时它建在 `asyncio.Semaphore` 上，线程里拿不到），实测：

        闸门 limit=4，4 个来源并行 → 真实并发峰值 16

    与本项目当年"配置写 4、实际 128"是同一个失效模式，只是发生在同步这一侧；
    `origin/develop` 上用同样探针测得同样的 16，说明它早于那次重构。

    这条用例曾是 `xfail(strict=True)`，混合闸门做出来后自动 XPASS 提醒摘标记
    —— 那正是当初标它的目的：把缺口钉成会自己响的闹钟，而不是写进注释等人忘记。
    """
    limit = 4
    model = _SyncCounting()
    model.bind_gate(Gate(limit=limit))

    def one_source() -> None:
        batch.embed_documents(model, [f"d{i}" for i in range(8)], max_concurrency=limit)

    threads = [threading.Thread(target=one_source) for _ in range(limit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert model.peak <= limit, f"闸门 limit={limit}，同步路径实际并发峰值 {model.peak}"


async def test_sync_media_also_goes_through_the_gate() -> None:
    """**同步多模态入口也不能绕过预算。**

    修 #44 时只把文本入口接上了闸门，`embed_media` 仍是可覆写的抽象方法，
    Qwen 的实现直接调未加闸的 `embed` —— 于是同步图片请求整条路绕开了预算，
    而图片恰恰比文本重得多（评审指出）。
    """
    model = RecordingEmbedding(delay=0.02)
    model.bind_gate(Gate(limit=2))
    media = MediaResource(url="https://example.invalid/a.png")

    await asyncio.gather(
        *[asyncio.to_thread(model.embed_media, media) for _ in range(8)]
    )

    assert model.peak <= 2, f"同步多模态绕过了闸门：峰值 {model.peak}"


def test_direct_embed_call_still_goes_through_the_gate() -> None:
    """**公开的 `embed()` 也不能是后门。**

    给 `embed_query` / `embed_document` / `embed_batch` / `embed_media` 都加了
    闸，却漏掉它们脚下这个公开入口，等于前门上锁、后门敞着。实测修复前：
    闸门 limit=2，直接调 `model.embed()` 真实峰值 8。
    """
    model = RecordingEmbedding(delay=0.02)
    model.bind_gate(Gate(limit=2))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(model.embed, "x") for _ in range(8)]
        for future in futures:
            future.result()  # 工作线程异常必须让测试失败，不能被 join() 吞掉

    assert model.peak <= 2, f"直接调 embed 绕过了闸门：峰值 {model.peak}"


def test_sync_rank_also_goes_through_the_gate() -> None:
    """**同步 `rank()` 也不能是后门。**

    embedding 与 loader 的同步入口都补过闸门了，reranker 这道原样留着 ——
    同一个疏漏第三次。实测修复前：闸门 limit=2，同步 rank 真实峰值 8。

    重排与嵌入抢的是同一块 GPU，所以它们共用同一个闸门；漏掉一边等于两边
    都白限。
    """
    reranker = RecordingReranker(RecordingEmbedding(delay=0.02))
    reranker.bind_gate(Gate(limit=2))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(reranker.rank, "q", ["d"]) for _ in range(8)]
        for future in futures:
            future.result()  # 传播 rank() 的异常，避免 peak 保持为 0 时假通过

    assert reranker.sink.peak <= 2, f"同步 rank 绕过了闸门：峰值 {reranker.sink.peak}"


@pytest.mark.parametrize("holding_mode", ["sync", "async"])
async def test_distinct_sync_and_async_instances_share_one_gate(
    holding_mode: str,
) -> None:
    """实例和客户端拆开后，同一资源的预算仍须双向互斥。"""
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release_sync = threading.Event()
    release_async = asyncio.Event()

    class BlockingSync(BaseEmbeddingModel):
        def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
            loop.call_soon_threadsafe(entered.set)
            assert release_sync.wait(2), "测试未释放同步请求"
            return [1.0]

    class BlockingAsync(AsyncBaseEmbeddingModel):
        async def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
            entered.set()
            await release_async.wait()
            return [2.0]

    sync_model, async_model = BlockingSync(), BlockingAsync()
    gate = Gate(limit=1, max_waiting=1, acquire_timeout=0)
    sync_model.bind_gate(gate)
    async_model.bind_gate(gate)
    holder = asyncio.create_task(
        asyncio.to_thread(sync_model.embed, "held")
        if holding_mode == "sync"
        else async_model.embed("held")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(Overloaded):
            if holding_mode == "sync":
                await asyncio.wait_for(async_model.embed("rejected"), 1)
            else:
                await asyncio.wait_for(
                    asyncio.to_thread(sync_model.embed, "rejected"), 1
                )
        assert gate.stats.in_flight == 1
    finally:
        release_sync.set()
        release_async.set()
        await holder

    assert gate.stats.in_flight == 0
    assert gate.stats.admitted == 1
    assert gate.stats.rejected == 1
