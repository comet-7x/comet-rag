"""基准测试的采集与归档（spec S4-6）。

## 这些数字量的是什么

**默认量的是框架开销，不是模型性能。** 替身模型瞬间返回，所以耗时里剩下的
全是本项目自己的部分：任务状态机的每一次落库、向量库读写、序列化、
路由与依赖注入。这正是我们想盯住的东西 —— 谁给每个 chunk 多加一次数据库
往返，这里立刻会看出来。

反过来说，**这些数字不能用来回答"我的服务能扛多少 QPS"**：那取决于 GPU、
模型和文档，跟本项目关系不大。把两者混为一谈是性能数字最常见的误读方式，
所以在 `docs/benchmark.md` 里也写了同一句话。

有一条例外：`test_embedding_overlaps_io` 故意给替身加了固定延迟，量的是
**并发是否真的重叠**（S4-3）。那条的判据是结构性的（串行 vs 并发差一个数量级），
不是"多少毫秒"，所以可以断言。

## 为什么不用 pytest-benchmark

它面向同步微基准，会对同一个函数反复校准重跑；而这里的每次测量都带状态
（入库会写库、建 collection），重跑会互相污染。而且它报的是 mean/median/stddev，
没有 P95/P99 —— 验收标准要的恰恰是后者。自己写一个三十行的采集器更合适。

## 用法

    uv run pytest -m benchmark                     # 跑并写出 bench-report.json
    uv run pytest -m benchmark --bench-out a.json
    uv run pytest -m benchmark --bench-baseline a.json   # 与上次对比，打印增减

**不对绝对耗时做断言**：机器一换数字就变，那种用例只会训练出"红了就重跑"
的习惯。回归靠 `--bench-baseline` 的对比来发现，是人看的，不是 CI 判的。
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.benchmark

DEFAULT_OUT = Path("bench-report.json")


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("comet-bench")
    group.addoption(
        "--bench-out",
        default=str(DEFAULT_OUT),
        help="基准结果写到哪里（JSON，用于归档与后续对比）",
    )
    group.addoption(
        "--bench-baseline",
        default=None,
        help="与这份历史结果对比并打印增减（PR 前后对比用）",
    )


# ── 采集 ───────────────────────────────────────────────────────────────────


def percentile(values: list[float], q: float) -> float:
    """第 q 百分位（0–100）。

    刻意用"最近秩"而不是插值：样本量小的时候插值出来的 P99 更像是数学产物，
    而"排序后第几个"至少是一次真实发生过的耗时。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(q / 100 * len(ordered) + 0.5) - 1))
    return ordered[k]


@dataclass(slots=True)
class Result:
    name: str
    metric: str
    unit: str
    value: float
    samples: int = 1
    detail: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metric": self.metric,
            "unit": self.unit,
            "value": round(self.value, 4),
            "samples": self.samples,
            "detail": {k: round(v, 4) for k, v in self.detail.items()},
        }


class Recorder:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def add(self, result: Result) -> None:
        self.results.append(result)


class Bench:
    """一个用例一个。`measure` 跑 N 次取分位数，`record` 记单个标量。"""

    def __init__(self, name: str, recorder: Recorder) -> None:
        self.name = name
        self._recorder = recorder

    async def measure(
        self,
        rounds: int,
        fn: Callable[[int], Awaitable[Any]],
        *,
        warmup: int = 1,
        metric: str = "latency",
    ) -> list[float]:
        """跑 `rounds` 轮，返回每轮耗时（毫秒）。

        `warmup` 轮不计入：第一次调用要建 collection、填缓存、编译正则，
        把它算进 P50 会让分布整个偏移，而那一次开销在真实负载里可以忽略。
        """
        for i in range(warmup):
            await fn(-1 - i)

        samples: list[float] = []
        for i in range(rounds):
            start = time.perf_counter()
            await fn(i)
            samples.append((time.perf_counter() - start) * 1000)

        self._recorder.add(
            Result(
                name=self.name,
                metric=metric,
                unit="ms",
                value=percentile(samples, 50),
                samples=len(samples),
                detail={
                    "p50": percentile(samples, 50),
                    "p95": percentile(samples, 95),
                    "p99": percentile(samples, 99),
                    "mean": statistics.fmean(samples),
                    "min": min(samples),
                    "max": max(samples),
                },
            )
        )
        return samples

    def record(self, metric: str, value: float, unit: str, **detail: float) -> None:
        self._recorder.add(
            Result(name=self.name, metric=metric, unit=unit, value=value, detail=detail)
        )


@pytest.fixture(scope="session")
def bench_recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def bench(request: pytest.FixtureRequest, bench_recorder: Recorder) -> Bench:
    return Bench(request.node.name, bench_recorder)


# ── 归档与对比 ─────────────────────────────────────────────────────────────


def _git_revision() -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 —— 不在 git 仓库里也要能跑
        return "unknown"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    recorder = getattr(session, "_comet_recorder", None)
    if recorder is None or not recorder.results:
        return

    report = {
        "meta": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "git": _git_revision(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
            # 机器信息必须一起存：跨机器比数字是没有意义的
            "note": "替身模型下的框架开销，不代表真实服务的吞吐",
        },
        "results": [r.to_dict() for r in recorder.results],
    }
    out = Path(session.config.getoption("--bench-out"))
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = ["", f"基准结果已写入 {out}", ""]
    lines.extend(_format_table(report["results"]))

    baseline_path = session.config.getoption("--bench-baseline")
    if baseline_path:
        lines.extend(_format_delta(report["results"], Path(baseline_path)))
    print("\n".join(lines))  # noqa: T201 —— 基准报告就是要打给人看的


def _format_table(results: list[dict[str, Any]]) -> list[str]:
    lines = [f"{'用例':<44} {'指标':<12} {'值':>10} {'p95':>10} {'p99':>10}"]
    lines.append("-" * 92)
    for r in results:
        d = r["detail"]
        lines.append(
            f"{r['name']:<44} {r['metric']:<12} "
            f"{r['value']:>9.2f}{r['unit']:<1} "
            f"{d.get('p95', 0):>9.2f} {d.get('p99', 0):>9.2f}"
        )
    return lines


def _format_delta(results: list[dict[str, Any]], baseline_path: Path) -> list[str]:
    if not baseline_path.exists():
        return ["", f"⚠️ 基线文件不存在：{baseline_path}"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    before = {(r["name"], r["metric"]): r["value"] for r in baseline.get("results", [])}

    lines = ["", f"与 {baseline_path}（{baseline['meta'].get('git')}）对比：", ""]
    for r in results:
        key = (r["name"], r["metric"])
        if key not in before:
            lines.append(f"  {r['name']:<44} 新增")
            continue
        old, new = before[key], r["value"]
        if old == 0:
            continue
        delta = (new - old) / old * 100
        # 耗时越低越好、吞吐越高越好 —— 方向搞反的话解读就整个反了
        better = delta < 0 if r["unit"] == "ms" else delta > 0
        mark = "✅" if abs(delta) < 5 else ("🟢" if better else "🔴")
        lines.append(
            f"  {mark} {r['name']:<42} {old:>9.2f} → {new:>9.2f}  ({delta:+.1f}%)"
        )
    return lines


def pytest_configure(config: pytest.Config) -> None:
    config._comet_bench_out = config.getoption("--bench-out")  # type: ignore[attr-defined]


@pytest.fixture(autouse=True, scope="session")
def _attach_recorder(request: pytest.FixtureRequest, bench_recorder: Recorder) -> None:
    """把 recorder 挂到 session 上，`pytest_sessionfinish` 才拿得到它。"""
    request.session._comet_recorder = bench_recorder  # noqa: SLF001
