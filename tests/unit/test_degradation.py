"""分级降级（spec S4-5）。

顺序、滞回、留痕三件事各自都能单独坏掉，且坏了都**不报错**：
  · 顺序错 → 资源紧张时先砍了写入，读路径照样慢，砍了个寂寞；
  · 没滞回 → 指标在阈值边界来回时 rerank 反复开关，日志刷屏、行为不可预测；
  · 不留痕 → 线上检索质量悄悄下降，没人知道发生过什么。
"""

from __future__ import annotations

import pytest

from comet_rag.core.degradation import (
    DegradationController,
    DegradationSettings,
    Level,
)


class FakeClock:
    """注入时钟：冷却期是 30 秒，测试不该真等。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make(**overrides) -> tuple[DegradationController, FakeClock]:
    clock = FakeClock()
    settings = DegradationSettings(min_samples=10, window=100, **overrides)
    return DegradationController(settings, clock=clock), clock


def feed(ctrl: DegradationController, *, failures: int, total: int) -> None:
    for i in range(total):
        ctrl.record(i >= failures)


# ── 判级 ───────────────────────────────────────────────────────────────────


def test_healthy_service_stays_at_normal() -> None:
    ctrl, _ = make()
    feed(ctrl, failures=0, total=50)
    assert ctrl.level() is Level.NORMAL
    assert ctrl.allow_rerank()
    assert ctrl.adjust_top_k(10) == 10
    assert ctrl.accept_writes()


def test_too_few_samples_never_degrades() -> None:
    """头几次调用失败不代表服务挂了。样本不足就判降级会让服务刚启动、
    或流量很低时莫名其妙地劣化。"""
    ctrl, _ = make()
    for _ in range(5):  # < min_samples
        ctrl.record(False)
    assert ctrl.level() is Level.NORMAL
    assert ctrl.failure_rate == 0.0


@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        (25, Level.NO_RERANK),  # 25% ≥ 20%
        (60, Level.LOWER_TOP_K),  # 60% ≥ 50%
        (90, Level.REJECT_WRITES),  # 90% ≥ 80%
    ],
)
def test_failure_rate_drives_the_level(failures: int, expected: Level) -> None:
    ctrl, _ = make()
    feed(ctrl, failures=failures, total=100)
    assert ctrl.level() is expected


def test_saturation_alone_can_trigger_degradation() -> None:
    """**只看失败率是不够的。**

    模型服务健康但请求量暴涨时，队列越排越长而失败率还没上来 ——
    等它上来时早就雪崩了。所以排队深度是独立的一路判据。
    """
    ctrl, _ = make()
    feed(ctrl, failures=0, total=50)  # 一次都没失败
    assert ctrl.level() is Level.NORMAL

    ctrl.observe_saturation(waiting=60, limit=8)  # 饱和度 7.5 ≥ 5.0
    assert ctrl.level() is Level.LOWER_TOP_K


# ── 降级动作 ───────────────────────────────────────────────────────────────


def test_degradation_order_is_rerank_then_top_k_then_writes() -> None:
    """**先砍最贵、最可有可无的。**

    rerank 是交叉编码器，读路径上最重的一步，而没有它检索仍然可用。
    拒绝写入放最后 —— 那是唯一让用户"什么也得不到"的一级。
    顺序反了的话，资源紧张时会先砍掉写入，而读路径照样慢。
    """
    ctrl, _ = make()

    feed(ctrl, failures=25, total=100)  # L1
    assert not ctrl.allow_rerank(), "L1 应当关掉 rerank"
    assert ctrl.adjust_top_k(10) == 10, "L1 不该动 top_k"
    assert ctrl.accept_writes(), "L1 不该拒写"

    ctrl, _ = make()
    feed(ctrl, failures=60, total=100)  # L2
    assert not ctrl.allow_rerank()
    assert ctrl.adjust_top_k(10) == 5, "L2 应当砍 top_k"
    assert ctrl.accept_writes(), "L2 仍不该拒写"

    ctrl, _ = make()
    feed(ctrl, failures=90, total=100)  # L3
    assert not ctrl.accept_writes(), "L3 才拒写"


def test_top_k_never_drops_below_one() -> None:
    """砍到 0 就等于"检索不返回任何东西"，那不是降级，是故障。"""
    ctrl, _ = make()
    feed(ctrl, failures=60, total=100)
    assert ctrl.adjust_top_k(1) == 1


# ── 滞回与恢复 ─────────────────────────────────────────────────────────────


def test_upgrade_is_immediate_but_recovery_waits() -> None:
    """升级立刻、降级要冷却 —— 否则指标在阈值边界来回时会疯狂抖动。"""
    ctrl, clock = make(recover_after=30.0)
    feed(ctrl, failures=60, total=100)
    assert ctrl.level() is Level.LOWER_TOP_K

    ctrl._outcomes.clear()  # noqa: SLF001 —— 模拟"指标已回落"
    feed(ctrl, failures=0, total=50)

    assert ctrl.level() is Level.LOWER_TOP_K, "指标刚回落就恢复了，没有冷却期"

    clock.advance(31)
    assert ctrl.level() is Level.NO_RERANK, "过了冷却期应当降一级"
    clock.advance(31)
    assert ctrl.level() is Level.NORMAL


def test_recovery_is_one_level_at_a_time() -> None:
    """一次只恢复一级：直接跳回正常，很容易立刻又被打回去，反而更抖。"""
    ctrl, clock = make(recover_after=1.0)
    feed(ctrl, failures=90, total=100)
    assert ctrl.level() is Level.REJECT_WRITES

    ctrl._outcomes.clear()  # noqa: SLF001
    feed(ctrl, failures=0, total=50)
    clock.advance(2)

    assert ctrl.level() is Level.LOWER_TOP_K  # 不是直接跳到 NORMAL


def test_flapping_metrics_do_not_flip_the_level_back_and_forth() -> None:
    """反向验证滞回：指标在阈值边界反复横跳，级别不该跟着抖。"""
    ctrl, clock = make(recover_after=30.0)
    for _ in range(5):
        ctrl._outcomes.clear()  # noqa: SLF001
        feed(ctrl, failures=25, total=100)  # 越线
        ctrl.level()
        ctrl._outcomes.clear()  # noqa: SLF001
        feed(ctrl, failures=0, total=100)  # 回落
        ctrl.level()
        clock.advance(1)  # 远不到冷却期

    assert ctrl.transitions.get("NO_RERANK", 0) == 1, (
        f"级别翻转了 {ctrl.transitions} 次 —— 没有滞回，rerank 在反复开关"
    )


# ── 留痕 ───────────────────────────────────────────────────────────────────


def test_every_level_change_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """S4-5 明确要求降级必须打日志 —— 否则线上质量下滑无人察觉。"""
    from comet_rag.core.logging import logger  # noqa: PLC0415

    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    try:
        ctrl, _ = make()
        feed(ctrl, failures=60, total=100)
        ctrl.level()
    finally:
        logger.remove(sink_id)

    assert any("服务降级" in r for r in records), f"降级没有留下日志：{records}"
    assert any("LOWER_TOP_K" in r for r in records)


def test_logging_happens_on_change_not_on_every_call() -> None:
    """只在**级别变化**时打日志。每次调用都打的话，过载时日志本身就成了
    新的负担 —— 而过载正是最需要日志还能用的时候。"""
    from comet_rag.core.logging import logger  # noqa: PLC0415

    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    try:
        ctrl, _ = make()
        feed(ctrl, failures=60, total=100)
        for _ in range(100):
            ctrl.level()
    finally:
        logger.remove(sink_id)

    assert len(records) == 1, f"级别没变却打了 {len(records)} 条日志"
