"""worker 进程的共享装配与生命周期。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from comet_rag.composition.bootstrap import build_context
from comet_rag.config.schemas import APPConfig, Backend
from comet_rag.config.settings import get_config
from comet_rag.core.logging import logger, setup_logging
from comet_rag.infrastructure.task_execution.arq import LANE_QUEUES, run_task
from comet_rag.workers.maintenance import DEFAULT_LEASE, sweep_cron


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
    #: 是否挂租约回收定时器。默认挂 —— arq 的 cron 是 unique 的，多副本下
    #: 每个时刻只有一个真的执行，不必为它单开一个进程。见 maintenance.py。
    sweep: bool = True

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
        # 单进程模式（InProcessExecutor）永远走不到这里 —— 它根本不加载
        # workers/，所以"单进程不得启用回收"是结构上保证的，不靠开关。
        cron_jobs = [sweep_cron()] if profile.sweep else []  # noqa: RUF012
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
            "lease": DEFAULT_LEASE,
        }
        on_startup = staticmethod(on_startup)
        on_shutdown = staticmethod(on_shutdown)

    WorkerSettings.__name__ = f"{profile.name.title()}WorkerSettings"
    return WorkerSettings


__all__ = ["WorkerProfile", "build_settings", "on_shutdown", "on_startup"]
