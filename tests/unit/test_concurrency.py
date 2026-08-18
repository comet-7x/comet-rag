"""并发闸门与有界背压（spec S4-1、S4-2）。

本文件盯着一个**实测过的真实缺陷**：闸门原本是"每次调用建一个信号量"，
于是 32 个任务各开 4 路扇出时，对模型服务的并发峰值是 **128**，而配置写的
是 4。配置说 4、实际 128，且监控上完全看不出来 —— 每个任务都觉得自己很守规矩。
"""

from __future__ import annotations

import asyncio
import threading
import time
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

    def embed(self, data: Any, **kwargs: Any) -> list[float]:  # pragma: no cover
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

    def embed_media(self, data: Any, /, **kwargs: Any) -> list[float]:  # pragma: no cover
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

    def score(self, query: Any, documents: Any, **kwargs: Any):  # pragma: no cover
        return []

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


# ── 已知缺口：同步路径不受进程级闸门约束 ───────────────────────────────────


class _SyncCounting(BaseEmbeddingModel):
    """只数同步 `embed` 的真实并发峰值。"""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0

    def embed(self, data: str, /, **kwargs: Any) -> list[float]:
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(self.delay)
        with self._lock:
            self.live -= 1
        return [0.0]

    async def _aembed(self, data: str, /, **kwargs: Any) -> list[float]:
        return [0.0]


@pytest.mark.xfail(
    strict=True,
    reason="同步路径尚未接入进程级闸门（#44）；本用例即该 issue 的验收标准",
)
def test_sync_fanout_should_respect_the_process_gate() -> None:
    """**同步扇出目前不受闸门约束，多个来源并行时并发会相乘。**

    `Pipeline.batch_run` 用 `max_concurrency` 个线程跑来源，每个来源内部的
    `embed_documents` 又开 `max_concurrency` 路 —— 而同步 `embed` 不经过
    asyncio 闸门（它是协程原语，线程里用不了）。于是实测：

        闸门 limit=4，4 个来源并行 → 对模型服务的真实并发峰值 16

    这正是本项目当年实测出的"配置写 4、实际 128"同一个失效模式，只是发生在
    同步这一侧。**该缺陷早于本次重构**：在 `origin/develop` 上用同样的探针
    （那边入口叫 `batch_embed`）测得同样的 16。

    修它需要一个**线程与协程共用同一份预算**的限流器：现在的 `Gate` 建立在
    `asyncio.Semaphore` 上，线程里拿不到；而各配一个信号量等于没限 —— 同步 4
    加异步 4，服务端看到 8，正是 `test_embedding_and_reranker_share_one_gate`
    反对的那件事。

    所以这里用 `xfail(strict=True)` 钉住：它现在必然失败，等混合闸门做出来会
    自动变绿并提醒去掉标记 —— 而不是把缺口写进注释里等人忘记。
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
