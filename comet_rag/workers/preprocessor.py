"""预处理 worker：取源 → 解析 → 清洗 → 分块（`LANE_CPU`）。

    uv run arq comet_rag.workers.preprocessor.WorkerSettings

## 并发只开 2，不是保守，是 GIL 决定的

解析 docx / 提取表格公式是**纯 Python 的 CPU 活**。`asyncio.to_thread` 只把它
挪出事件循环，让心跳与 `ctx.checkpoint()` 还能跑得动；**并不能真的并行** ——
线程之间还在抢同一个 GIL。

于是 `max_jobs` 开大只有坏处：吞吐一点没涨，每个任务的耗时却被拉长到 N 倍，
p99 和心跳一起劣化，`sweep_stale` 甚至可能误判为租约过期。

**扩容方式是加进程**（k8s 多副本 / supervisor 多实例 / 多开几个上面那条命令），
副本数 ≈ 可用 CPU 核数。arq 本身不提供 `--workers`，多开就是多起几个进程 ——
它们抢同一条 Redis 队列，天然负载均衡。

为什么不用 `ProcessPoolExecutor` 在进程内并行？试过这条路的人都会撞上同一堵墙：
extractor 是运行时注册进 `PipelineHooks` 的可调用对象，跨进程要可 pickle；
而且 worker 进程本来就是按副本扩的，再套一层进程池只是把同一件事做两遍，
却多出一套需要单独调参与监控的东西。
"""

from __future__ import annotations

from typing import Any

from comet_rag.tasks import LANE_CPU
from comet_rag.workers.base import WorkerProfile, build_settings

PROFILE = WorkerProfile(
    name="preprocessor",
    lane=LANE_CPU,
    # 2 而非 1：留一个槽位，让一个任务在等磁盘/网络取源时另一个能占住 CPU。
    # 再大就纯属让任务互相拖慢了。
    max_jobs=2,
    scaling="加进程（副本数 ≈ CPU 核数）；**不要**调大 max_jobs",
    # 解析一份大 docx 可能几分钟，但超过半小时基本就是卡死了
    job_timeout=1800.0,
)


def __getattr__(name: str) -> Any:
    """惰性构造 `WorkerSettings`（PEP 562，与 `api/main.py:app` 同一套理由）。

    写成模块级常量的话，"import 这个模块"就等价于"必须存在一份合法配置"，
    `test_importable` 与任何静态检查都会被一份缺字段的 config.yaml 拦住。
    arq 的 CLI 用 `getattr(module, "WorkerSettings")` 取它，所以这里能接上。
    """
    if name == "WorkerSettings":
        return build_settings(PROFILE)
    raise AttributeError(name)


__all__ = ["PROFILE", "WorkerSettings"]  # noqa: F822 —— WorkerSettings 由 __getattr__ 提供
