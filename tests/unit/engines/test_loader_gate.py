"""加载侧的进程级闸门。

模型侧实测出过"配置写 4、实际并发 128"：每个任务各自守规矩，加起来不守。
加载侧此前**完全没有**闸门，而 `ingestion.py` 每个任务调一次 `aload`，
`max_jobs=32` 就是 32 路对外抓取，没有任何上限。
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.engines.loaders.auto_loader import AutoLoader, LoaderRoute
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent


class CountingLoader(BaseLoader):
    """记录真实并发峰值的替身。峰值是唯一要看的那个数。"""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.live = 0
        self.peak = 0
        self.calls = 0

    def _load(
        self, source: SourceContent | str, **kwargs: Any
    ) -> LoaderContent:  # pragma: no cover
        raise NotImplementedError

    async def _aload(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        self.calls += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.live -= 1
        return LoaderContent(path=Path("/dev/null"), source=SourceContent("x"))

    def cleanup(self) -> None:
        return None


async def test_process_gate_caps_loading_no_matter_how_many_tasks() -> None:
    """**任务数再多也不该突破加载上限。**

    不挂闸门时，32 个并发任务就是 32 路抓取 —— 每个任务都觉得自己只发了一个
    请求。真正该被约束的是**进程对外的总连接数**。
    """
    loader = CountingLoader()
    loader.bind_gate(Gate(limit=4))

    await asyncio.gather(*(loader.aload(SourceContent("s")) for _ in range(32)))

    assert loader.peak <= 4, f"并发峰值 {loader.peak} 超过闸门上限 4"
    assert loader.peak > 1, "峰值恒为 1 说明根本没并行，这条用例测了个寂寞"
    assert loader.calls == 32, "闸门不该吞掉任何一次加载"


async def test_without_a_gate_loading_is_unbounded() -> None:
    """对照组：没有闸门就是修复前的行为，用来证明上一条不是自证。"""
    loader = CountingLoader()

    await asyncio.gather(*(loader.aload(SourceContent("s")) for _ in range(32)))

    assert loader.peak > 4, "没挂闸门却被限住了，那上一条用例证明不了什么"


async def test_gate_is_not_bypassable_by_subclasses() -> None:
    """`aload` 是 final，子类只能实现 `_aload` —— 结构上没有绕过的写法。"""
    assert getattr(BaseLoader.aload, "__final__", False)
    assert getattr(BaseLoader._aload, "__isabstractmethod__", False)  # noqa: SLF001


# ── AutoLoader 不得双重获取 ────────────────────────────────────────────────


def _auto(loader: BaseLoader) -> AutoLoader:
    return AutoLoader([LoaderRoute(name="all", matcher=lambda _s: True, loader=loader)])


async def test_auto_loader_forwards_the_gate_instead_of_holding_it() -> None:
    """**路由器持有闸门会让一次加载取两次许可。**

    `AutoLoader._aload` 只做匹配，真正的抓取在叶子那层。两层都持有的话，
    外层拿到许可后内层还要再要一次 —— 上限为 1 时当场死锁，上限大时也白白
    吃掉一半名额。
    """
    leaf = CountingLoader()
    auto = _auto(leaf)
    gate = Gate(limit=1)

    auto.bind_gate(gate)

    assert auto._gate is None, "AutoLoader 不该自己持有闸门"  # noqa: SLF001
    assert leaf._gate is gate, "闸门没有转发到叶子 loader"  # noqa: SLF001

    # 上限 1 时最容易暴露死锁：两层都持有的话这里会永远等下去
    async with asyncio.timeout(5):
        await auto.aload(SourceContent("s"))

    assert leaf.calls == 1
    assert gate.stats.admitted == 1, "一次加载只该取一次许可"


async def test_routing_through_auto_loader_still_respects_the_cap() -> None:
    leaf = CountingLoader()
    auto = _auto(leaf)
    auto.bind_gate(Gate(limit=3))

    async with asyncio.timeout(10):
        await asyncio.gather(*(auto.aload(SourceContent("s")) for _ in range(24)))

    assert leaf.peak <= 3, f"经 AutoLoader 路由后峰值 {leaf.peak} 超过 3"
    assert leaf.calls == 24


@pytest.mark.parametrize("limit", [1, 2])
async def test_batch_load_also_goes_through_the_gate(limit: int) -> None:
    """批量入口的 `max_concurrency` 与闸门叠加，取两者中更紧的那个。"""
    leaf = CountingLoader()
    auto = _auto(leaf)
    auto.bind_gate(Gate(limit=limit))

    sources: list[Any] = [SourceContent(f"s{i}") for i in range(12)]
    async with asyncio.timeout(10):
        await auto.abatch_load(sources, max_concurrency=8)

    assert leaf.peak <= limit, f"闸门 {limit}，实际峰值 {leaf.peak}"


# ── 批量路径同样不能绕过闸门（评审指出）─────────────────────────────────────


async def test_url_batch_workers_also_go_through_the_gate() -> None:
    """**批量 worker 直接调实现方法，于是绕开了 `load` 上的闸门。**

    `URLLoader` 的批量刻意让 worker 直接调 `_load_impl`（避免嵌套登记活动操作
    导致 cleanup 循环等待）。绕开的本该只是"登记"，结果连限流一起绕了 ——
    `AutoLoader.batch_load` 一路下来，`max_concurrency` 个 worker 能同时打到
    下游，哪怕闸门配得更小。
    """
    from comet_rag.engines.loaders.url_loader import URLLoader

    gate = Gate(limit=2)
    loader = URLLoader()
    loader.bind_gate(gate)

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_impl(source: Any, **_: Any) -> LoaderContent:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        return LoaderContent(path=Path("/dev/null"), source=SourceContent("x"))

    loader._load_impl = fake_impl  # type: ignore[method-assign]  # noqa: SLF001
    try:
        await asyncio.to_thread(
            loader.batch_load,
            [f"https://example.invalid/{i}" for i in range(12)],
            max_concurrency=8,
        )
    finally:
        loader.cleanup()

    assert peak <= 2, f"批量 worker 绕过了闸门：峰值 {peak} 超过上限 2"
    assert peak > 1, "峰值恒为 1 说明没并行，这条用例测了个寂寞"


# ── 厂商专用选项必须能穿过模板方法 ─────────────────────────────────────────


@pytest.mark.parametrize("entry", ["load", "aload"])
async def test_loader_specific_options_survive_the_template_method(entry: str) -> None:
    """**`docs/pipeline_usage.md` 教用户直接调 `URLLoader` 传专用选项。**

    模板方法若只收 `source`，那条文档就地失效 —— 实测报
    `BaseLoader.load() got an unexpected keyword argument 'download_config'`。
    闸门是加在中间的，不该把参数吃掉。
    """
    from comet_rag.engines.loaders.url_loader import DownloadRequestConfig, URLLoader

    loader = URLLoader()
    seen: list[Any] = []

    def fake_sync(source: Any, **kwargs: Any) -> LoaderContent:
        seen.append(kwargs.get("download_config"))
        return LoaderContent(path=Path("/dev/null"), source=SourceContent("x"))

    async def fake_async(source: Any, **kwargs: Any) -> LoaderContent:
        return fake_sync(source, **kwargs)

    loader._load_impl = fake_sync  # type: ignore[method-assign]  # noqa: SLF001
    loader._aload_impl = fake_async  # type: ignore[method-assign]  # noqa: SLF001
    config = DownloadRequestConfig(timeout=5)
    try:
        if entry == "load":
            loader.load("https://example.invalid/a", download_config=config)
        else:
            await loader.aload("https://example.invalid/a", download_config=config)
    finally:
        loader.cleanup()

    assert seen == [config], "download_config 没有透传到实现层"


@pytest.mark.parametrize("entry", ["load", "aload"])
async def test_unknown_options_are_still_rejected(entry: str) -> None:
    """转发不等于放行：拼错的参数名必须当场报错，不能悄悄忽略。"""
    from comet_rag.engines.loaders.url_loader import URLLoader

    loader = URLLoader()
    try:
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            if entry == "load":
                loader.load("https://example.invalid/a", typo=True)
            else:
                await loader.aload("https://example.invalid/a", typo=True)
    finally:
        loader.cleanup()


async def test_local_loader_takes_exactly_one_permit_per_load() -> None:
    """**`_aload` 委派时必须调未加闸的 `_load`。**

    `LocalLoader._aload` 曾是 `to_thread(self.load, source)` —— `self.load` 是
    加了闸的模板方法，于是一次加载取两次许可：实测 `admitted=2`，`limit=1` 时
    当场死锁。这与 `AutoLoader.bind_gate` 里说的是同一个失效模式，只是发生在
    另一个 loader 上。
    """
    from comet_rag.engines.loaders.local_loader import LocalLoader

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "a.txt"
        target.write_text("hi", encoding="utf-8")

        loader = LocalLoader()
        gate = Gate(limit=1)
        loader.bind_gate(gate)
        try:
            async with asyncio.timeout(5):
                await loader.aload(SourceContent(str(target)))
        finally:
            loader.cleanup()

    assert gate.stats.admitted == 1, f"一次加载取了 {gate.stats.admitted} 次许可"
    assert gate.stats.in_flight == 0
