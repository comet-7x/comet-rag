"""worker 进程的共用装配。

## 为什么按负载特征分 worker，而不是按业务名词

"解析 worker""向量化 worker"听起来是按业务分的，实际分对了 —— 但**分对的
理由不是业务，是负载**。真正的判据只有一条：**这类活该怎么扩容**。

    preprocessor（LANE_CPU）  解析 / 清洗 / 分块
        CPU 密集。GIL 决定了加协程不增吞吐，只让每个任务都变慢。
        扩容 = **加进程**（多开几个 arq 进程 / 多几个副本），理想是 ≈ CPU 核数。

    embedder（LANE_IO）       向量化 + 写向量库
        IO 密集。绝大部分时间在等模型服务与 Milvus 回包。
        扩容 = **单进程内加并发**（提高 max_jobs / 信号量）。

**扩容方式反过来会真的更慢**，而且症状具有欺骗性：
  · 给 embedder 加进程 → 对模型服务的连接数乘以进程数，请求在服务端排队，
    每个进程都以为是"上游慢"，看不出是自己造成的；
  · 给 preprocessor 加并发 → 一堆解析任务抢同一个 GIL，总吞吐不变，
    但每个任务的耗时被拉长到 N 倍，进而拖垮 p99 与心跳。

## 两个 worker 共享同一套装配

都走 `build_context()`，各自进程里只有**一份** httpx 连接池、一份向量库
连接、一份模型客户端 —— 这正是 spec A3 选 ARQ 的核心理由（S4-4：不重建
连接池与 TLS 握手）。各自 new 资源的话，连接数会随任务数线性涨。

## WorkerSettings 为什么是惰性的

与 `api/main.py:app` 同一个问题、同一个解法（PEP 562）：写成模块级常量的话，
"import 这个模块"就等价于"必须存在一份合法配置"，静态检查、文档守卫、
`test_importable` 全会被一份缺字段的 config.yaml 拦住。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from comet_rag.config.schemas import APPConfig, Backend
from comet_rag.config.settings import get_config
from comet_rag.core.bootstrap import build_context
from comet_rag.core.logging import logger, setup_logging
from comet_rag.tasks.executor_arq import LANE_QUEUES, run_task


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    """一类 worker 的负载画像。**并发参数与扩容方式必须一起看**，
    单看 `max_jobs` 会得出"embedder 配得太激进"的错误结论。"""

    name: str
    lane: str
    max_jobs: int
    #: 人读的扩容说明，启动时打进日志 —— 运维改副本数时最需要看到的就是它
    scaling: str
    job_timeout: float = 1800.0

    @property
    def queue(self) -> str:
        return LANE_QUEUES[self.lane]


async def on_startup(ctx: dict[str, Any]) -> None:
    """装配本进程的全套资源。arq 在起 worker 时调一次。"""
    profile: WorkerProfile = ctx["profile"]
    setup_logging(module_files={"workers": "worker", "services": "services"})

    config: APPConfig = ctx.get("config") or get_config()
    context = build_context(
        config, executor_lane=profile.lane, **ctx.get("build_kwargs", {})
    )
    ctx["context"] = context
    #: `run_task` 从这里取执行器。它与 API 进程里那个是**不同实例**，
    #: 之间只靠 Redis 与 TaskStore 通信 —— 这正是跨进程该有的样子。
    ctx["executor"] = context.task_executor

    logger.info(
        f"worker 已就绪 name={profile.name} lane={profile.lane} "
        f"queue={profile.queue} max_jobs={profile.max_jobs}｜扩容方式：{profile.scaling}"
    )
    if config.backends.task_executor is not Backend.ARQ:
        # 配成 inprocess 却起了 arq worker：任务会被 API 进程自己跑掉，
        # 这个 worker 空转到天荒地老。宁可吵一句也别让人对着空队列查半天。
        logger.warning(
            "backends.task_executor 不是 arq，本 worker 大概率永远收不到任务"
        )


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """逆序释放。`Context.aclose()` 会先停执行器（让在途任务落到一致状态），
    再拆下游连接 —— 顺序反了会让在跑的任务撞上"连接已关闭"。"""
    context = ctx.get("context")
    if context is not None:
        await context.aclose()
        logger.info(f"worker 已关停 name={ctx['profile'].name}")


def build_settings(
    profile: WorkerProfile,
    *,
    config: APPConfig | None = None,
    **build_kwargs: Any,
) -> type:
    """产出 arq 的 `WorkerSettings`。

    `config` 与 `build_kwargs` 显式传入时不读 config.yaml，并原样透传给
    `build_context` —— 与 `make_lifespan` 完全同一套口子。集成测试借此把
    假模型注进 worker 进程，于是测到的是**真实装配路径**，而不是另抄一份。
    """
    settings = (config or get_config()).infrastructure_config.redis
    if settings is None:
        raise ValueError("起 worker 需要 infrastructure_config.redis")

    from arq.connections import RedisSettings  # noqa: PLC0415

    class WorkerSettings:
        functions = [run_task]  # noqa: RUF012 —— arq 要求的就是普通类属性
        queue_name = profile.queue
        redis_settings = RedisSettings.from_dsn(settings.url)
        max_jobs = profile.max_jobs
        job_timeout = profile.job_timeout
        #: 重试归 TaskStore 管（attempts / max_attempts / 退避）。
        #: 让 arq 也插一脚会变成"库里记 2 次、实际跑了 6 次"，两套计数谁也不对。
        retry_jobs = False
        max_tries = 1
        #: 结果键只用来做入队去重，任务真正的结果在 TaskStore 里。
        #: 留太久会让 Redis 白占内存，留太短则去重窗口不够覆盖一次重投。
        keep_result = 300
        ctx = {  # noqa: RUF012
            "profile": profile,
            "config": config,
            "build_kwargs": build_kwargs,
        }
        on_startup = staticmethod(on_startup)
        on_shutdown = staticmethod(on_shutdown)

    WorkerSettings.__name__ = f"{profile.name.title()}WorkerSettings"
    return WorkerSettings


__all__ = ["WorkerProfile", "build_settings", "on_shutdown", "on_startup"]
