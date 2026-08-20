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
from comet_rag.engines.embedding.batch import aembed_documents, embed_documents
from comet_rag.infrastructure.providers.embedding.base import (
    BaseEmbeddingModel,
    MultimodalEmbeddingMixin,
)
from comet_rag.infrastructure.providers.reranker.base import BaseReranker
from comet_rag.ports import MediaResource


class RecordingEmbedding(MultimodalEmbeddingMixin, BaseEmbeddingModel):
    """记录**真实并发峰值**的替身。峰值是 S4-2 唯一要看的那个数。"""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.live = 0
        self.peak = 0
        self.calls = 0
        #: 同步侧在别的线程里跑，计数要自己护住
        self._sync_lock = threading.Lock()

    def _embed(self, data: Any, **kwargs: Any) -> list[float]:
        """同步文本：与多模态共用同一份计数，用来验证它也过闸门。"""
        with self._sync_lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(self.delay)
        with self._sync_lock:
            self.live -= 1
        return [0.0]

    async def _aembed(self, data: Any, **kwargs: Any) -> list[float]:
        self.calls += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.live -= 1
        return [0.0]

    def _embed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        """同步多模态：与文本共用同一份计数，用来验证它也过闸门。"""
        with self._sync_lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(self.delay)
        with self._sync_lock:
            self.live -= 1
        return [0.0]

    async def _aembed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        """多模态走同一份计数 —— 用来验证它与文本共用同一个闸门。"""
        return await self._aembed("<media>", **kwargs)

    async def close_client(self) -> None:
        return None


class RecordingReranker(BaseReranker):
    def __init__(self, sink: RecordingEmbedding) -> None:
        #: 与 embedding 共用一份计数，用来验"两者共用同一个闸门"
        self.sink = sink

    def _score(self, query: Any, documents: Any, **kwargs: Any) -> list[float]:
        """与 embedding 共用同一份计数，用来验证同步重排也过闸门。"""
        with self.sink._sync_lock:
            self.sink.live += 1
            self.sink.peak = max(self.sink.peak, self.sink.live)
        time.sleep(self.sink.delay)
        with self.sink._sync_lock:
            self.sink.live -= 1
        return [0.0] * len(list(documents))

    async def _ascore(self, query: Any, documents: Any, **kwargs: Any) -> list[float]:
        self.sink.live += 1
        self.sink.peak = max(self.sink.peak, self.sink.live)
        try:
            await asyncio.sleep(self.sink.delay)
        finally:
            self.sink.live -= 1
        return [0.0] * len(documents)


# ── Gate 本身 ──────────────────────────────────────────────────────────────


async def test_gate_caps_concurrency() -> None:
    gate = Gate(limit=3)
    live = peak = 0

    async def work() -> None:
        nonlocal live, peak
        async with gate:
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await asyncio.gather(*(work() for _ in range(30)))

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

    for _ in range(20):  # 制造 20 次超时
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
    32 个任务 × 每个 4 路 = 128 路真实并发，而配置写的是 4。
    """
    model = RecordingEmbedding()  # 不挂闸门 = 修复前的行为

    await asyncio.gather(
        *(aembed_documents(model, ["x"] * 8, max_concurrency=4) for _ in range(32))
    )

    assert model.peak > 4, (
        "没挂闸门却把并发压在了 4 以内 —— 那这条用例已经证明不了什么了"
    )


async def test_process_gate_caps_the_service_no_matter_how_many_tasks() -> None:
    """挂上进程级闸门后，任务数再多也不会突破上限（S4-2）。"""
    model = RecordingEmbedding()
    model.bind_gate(Gate(limit=4))

    await asyncio.gather(
        *(aembed_documents(model, ["x"] * 8, max_concurrency=4) for _ in range(32))
    )

    assert model.peak <= 4, f"并发峰值 {model.peak} 超过闸门上限 4"
    assert model.peak > 1, "并发度恒为 1 说明根本没并行，闸门测了个寂寞"
    assert model.calls == 32 * 8, "闸门不该吞掉任何一次调用"


async def test_embedding_and_reranker_share_one_gate() -> None:
    """两者抢同一块 GPU，各配一个闸门等于没限：两边都配 4，服务端看到 8。"""
    model = RecordingEmbedding()
    reranker = RecordingReranker(model)
    gate = Gate(limit=4)
    model.bind_gate(gate)
    reranker.bind_gate(gate)

    await asyncio.gather(
        aembed_documents(model, ["x"] * 20, max_concurrency=20),
        *(reranker.ascore("q", ["d"]) for _ in range(20)),
    )

    assert model.peak <= 4, f"两条路合起来的并发峰值 {model.peak} 超过 4"


async def test_gate_is_not_bypassable_by_subclasses() -> None:
    """`aembed` 是终态方法，子类只能实现 `_aembed` —— 于是绕不过闸门。

    做成"包一层装饰器"也能限流，但那只是约定：谁直接拿着模型调一下就绕过
    去了，且不会有任何报错。这条用例把"结构上做不到"钉住。
    """
    assert "aembed" not in vars(RecordingEmbedding), "子类覆写了 aembed —— 闸门被绕过了"
    assert getattr(BaseEmbeddingModel._aembed, "__isabstractmethod__", False)
    assert getattr(BaseReranker._ascore, "__isabstractmethod__", False)

    # 全仓的生产实现也不许覆写
    from comet_rag.infrastructure.providers.embedding.openai_embedding_model import (  # noqa: PLC0415
        OpenAIEmbeddingModel,
    )
    from comet_rag.infrastructure.providers.embedding.qwen3_vl_embedding import (  # noqa: PLC0415
        Qwen3VLEmbeddingModel,
    )
    from comet_rag.infrastructure.providers.reranker.qwen3_vl_reranker import (  # noqa: PLC0415
        Qwen3VLReranker,
    )

    for cls in (Qwen3VLEmbeddingModel, OpenAIEmbeddingModel):
        assert "aembed" not in vars(cls), f"{cls.__name__} 覆写了 aembed，绕过了闸门"
    assert "ascore" not in vars(Qwen3VLReranker), "Qwen3VLReranker 覆写了 ascore"


async def test_overload_propagates_instead_of_being_swallowed() -> None:
    """过载必须以异常形式传出去 —— 静默丢弃比报错糟得多（S4-1）。"""
    model = RecordingEmbedding(delay=0.05)
    model.bind_gate(Gate(limit=1, max_waiting=1))

    results = await asyncio.gather(
        *(model.aembed("x") for _ in range(6)), return_exceptions=True
    )

    rejected = [r for r in results if isinstance(r, Overloaded)]
    assert rejected, "闸门爆了却一个错都没报，调用方无从得知"
    assert (
        len(rejected) + len([r for r in results if not isinstance(r, BaseException)])
        == 6
    )


async def test_media_entry_is_gated_like_text() -> None:
    """**图片入口也必须走闸门。**

    图片请求通常比文本更重（一张图能顶几十倍 token），如果 `aembed_media`
    绕开闸门，限流就等于开了个后门：配了 4 并发，实际可以有 4 + N 个在飞。

    这条用例把文本与多模态混在一起打，只看**真实并发峰值**。
    """
    model = RecordingEmbedding(delay=0.02)
    gate = Gate(limit=2, max_waiting=64)
    model.bind_gate(gate)

    media = MediaResource(url="https://example.invalid/a.png")
    await asyncio.gather(
        *[model.aembed(f"text-{i}") for i in range(6)],
        *[model.aembed_media(media) for _ in range(6)],
    )

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

    async def _aembed(self, data: str, /, **kwargs: Any) -> list[float]:
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
        embed_documents(model, [f"d{i}" for i in range(16)], max_concurrency=limit)

    threads = [threading.Thread(target=one_source) for _ in range(limit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert model.peak <= limit, (
        f"闸门 limit={limit}，同步路径实际并发峰值 {model.peak}"
    )


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

    assert reranker.sink.peak <= 2, (
        f"同步 rank 绕过了闸门：峰值 {reranker.sink.peak}"
    )
