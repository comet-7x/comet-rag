"""分级降级（spec S4-5）：资源紧张时**主动降低服务质量，而不是集体变慢**。

    L0 正常
    L1 关掉 rerank        —— 检索结果略差，但延迟立刻降下来
    L2 再砍 top_k         —— 召回更少，向量库与序列化的开销同步下降
    L3 拒绝新的入库任务    —— 保住读路径；写路径本来就可以等

顺序不是随便排的：**先砍最贵、最可有可无的**。rerank 是交叉编码器，每次查询
要给几十个候选打分，是整条读路径上最重的一步，而没有它检索仍然可用（向量
召回本身就是完整答案）。top_k 次之。拒绝写入放最后 —— 那是唯一会让用户
"什么也得不到"的一级。

## 判据：超时率 + 闸门饱和度

两个信号缺一不可：
  · **只看超时率**：模型服务健康但请求量暴涨时，队列越排越长而超时率还没上来，
    等它上来时早就雪崩了；
  · **只看队列深度**：模型服务变慢（但没满）时队列可能并不深，却已经在拖 p99。

## 必须有滞回，否则会抖

阈值一刀切的话，指标在边界附近来回时 rerank 会被反复开关，行为不可预测、
日志刷屏。所以升级立刻生效、**降级要等冷却期**（`recover_after`），
并且只在**级别真的变化**时打日志 —— 每次调用都打的话，过载时日志本身
就成了新的负担。

## 观测点在闸门上

每一次对模型服务的调用都穿过 `Gate`，所以让 `Gate` 顺手把结果报上来，
不需要在每个调用点插桩 —— 与"闸门本身绕不过去"是同一个理由。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum

from comet_rag.core.logging import logger


class Level(IntEnum):
    """降级级别。用 `IntEnum` 是为了能写 `level >= Level.NO_RERANK`。"""

    NORMAL = 0
    NO_RERANK = 1
    LOWER_TOP_K = 2
    REJECT_WRITES = 3


@dataclass(frozen=True, slots=True)
class DegradationSettings:
    """触发条件。默认值偏保守 —— 宁可晚降一点，也别在正常抖动时乱降。"""

    #: 各级的超时/失败率阈值（最近窗口内）
    failure_rate: tuple[float, float, float] = (0.2, 0.5, 0.8)
    #: 各级的闸门饱和度阈值（等待者数 / 上限）
    saturation: tuple[float, float, float] = (2.0, 5.0, 10.0)
    #: 样本不足时一律判正常 —— 头两次调用失败不代表服务挂了
    min_samples: int = 20
    #: 统计窗口容量（最近多少次调用）
    window: int = 200
    #: 降级容易升级难：指标回落后要稳定这么久才降一级
    recover_after: float = 30.0
    #: L2 把 top_k 乘以这个系数
    top_k_factor: float = 0.5


class DegradationController:
    """观测 + 判级 + 留痕。**进程级单例**，由组合根建好挂进 `Context`。"""

    def __init__(
        self,
        settings: DegradationSettings | None = None,
        *,
        clock: object = time.monotonic,
    ) -> None:
        self._s = settings or DegradationSettings()
        self._clock = clock  # 注入时钟，测试不必真等冷却期
        self._outcomes: deque[bool] = deque(maxlen=self._s.window)
        self._level = Level.NORMAL
        self._level_since = self._now()
        self._saturation = 0.0
        #: 每一级累计触发次数，供 /admin/limits 观察
        self.transitions: dict[str, int] = {}

    def _now(self) -> float:
        return self._clock()  # type: ignore[operator]

    # ── 观测 ───────────────────────────────────────────────────────────────

    def record(self, ok: bool) -> None:
        """报一次模型调用的结果。由 `Gate` 自动调用，不需要手工插桩。"""
        self._outcomes.append(ok)

    def observe_saturation(self, waiting: int, limit: int) -> None:
        """报闸门的排队深度（等待者 / 上限）。"""
        self._saturation = waiting / limit if limit else 0.0

    @property
    def failure_rate(self) -> float:
        if len(self._outcomes) < self._s.min_samples:
            return 0.0
        return sum(1 for ok in self._outcomes if not ok) / len(self._outcomes)

    # ── 判级 ───────────────────────────────────────────────────────────────

    def _target_level(self) -> Level:
        rate, sat = self.failure_rate, self._saturation
        for level in (Level.REJECT_WRITES, Level.LOWER_TOP_K, Level.NO_RERANK):
            i = int(level) - 1
            if rate >= self._s.failure_rate[i] or sat >= self._s.saturation[i]:
                return level
        return Level.NORMAL

    def level(self) -> Level:
        """当前级别。升级立刻生效，降级要过冷却期（滞回，防抖）。"""
        target = self._target_level()
        if target > self._level:
            self._switch(target, "升级")
        elif target < self._level and (
            self._now() - self._level_since >= self._s.recover_after
        ):
            # 一次只降一级：直接跳回正常容易立刻又被打回去
            self._switch(Level(self._level - 1), "恢复")
        return self._level

    def _switch(self, level: Level, why: str) -> None:
        previous, self._level = self._level, level
        self._level_since = self._now()
        self.transitions[level.name] = self.transitions.get(level.name, 0) + 1
        # **只在级别真的变化时打日志**：每次调用都打的话，过载时日志本身
        # 就成了新的负担；而不打的话，线上质量下滑无人察觉（S4-5 明确要求）。
        logger.warning(
            f"服务降级{why}：{previous.name} → {level.name}"
            f"（失败率 {self.failure_rate:.0%}，闸门饱和度 {self._saturation:.1f}）"
        )

    # ── 供调用方使用 ───────────────────────────────────────────────────────

    def allow_rerank(self) -> bool:
        return self.level() < Level.NO_RERANK

    def adjust_top_k(self, top_k: int) -> int:
        if self.level() < Level.LOWER_TOP_K:
            return top_k
        return max(1, int(top_k * self._s.top_k_factor))

    def accept_writes(self) -> bool:
        return self.level() < Level.REJECT_WRITES

    @property
    def stats(self) -> dict[str, object]:
        return {
            "level": self._level.name,
            "failure_rate": round(self.failure_rate, 3),
            "saturation": round(self._saturation, 2),
            "samples": len(self._outcomes),
            "transitions": dict(self.transitions),
        }


__all__ = [
    "DegradationController",
    "DegradationSettings",
    "Level",
]
