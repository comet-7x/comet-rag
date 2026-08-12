"""worker 画像的不变量。零依赖，跟着单元测试跑。

这些断言看着琐碎，但它们守的是 T23 里唯一一句会造成**真实性能事故**的话：
"扩容方式反了会让模型服务过载排队，整体更慢"。参数一旦被谁顺手对调，
症状是"上游变慢"，没有任何测试会红 —— 除非有这一组。
"""

from __future__ import annotations

import pytest

from comet_rag.config.schemas import (
    APPConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    RedisConfig,
    ServerConfig,
)
from comet_rag.services.ingestion import IngestRunner
from comet_rag.tasks import LANE_CPU, LANE_IO, TaskContext
from comet_rag.tasks.executor_arq import LANE_QUEUES
from comet_rag.workers.base import build_settings
from comet_rag.workers.embedder import PROFILE as EMBEDDER
from comet_rag.workers.maintenance import DEFAULT_LEASE, sweep_cron
from comet_rag.workers.preprocessor import PROFILE as PREPROCESSOR

PROFILES = (PREPROCESSOR, EMBEDDER)


def test_the_two_profiles_serve_different_lanes() -> None:
    assert PREPROCESSOR.lane == LANE_CPU
    assert EMBEDDER.lane == LANE_IO


def test_each_lane_gets_its_own_queue() -> None:
    """共用队列的话，CPU 密集的活会被 IO worker 捞走，分道就白分了。"""
    queues = {p.queue for p in PROFILES}
    assert len(queues) == len(PROFILES), f"两条道共用了队列：{queues}"


def test_cpu_lane_runs_far_less_concurrently_than_io_lane() -> None:
    """**这条对调了就是事故**，所以单独立一条并写清理由。

    preprocessor 是 CPU 密集：GIL 决定了加协程不增吞吐，只把每个任务拖慢，
    连心跳都会被挤掉，`sweep_stale` 甚至可能误判租约过期。它靠**加进程**扩。
    embedder 是 IO 密集：时间都花在等回包上，单进程高并发才是对的；
    加进程只会把对模型服务的连接数乘上进程数，请求在服务端排队。
    """
    assert PREPROCESSOR.max_jobs < EMBEDDER.max_jobs, (
        f"preprocessor({PREPROCESSOR.max_jobs}) 的并发不该 ≥ "
        f"embedder({EMBEDDER.max_jobs}) —— 这两个值反了会让整体更慢"
    )
    assert PREPROCESSOR.max_jobs <= 4, "CPU 道开这么大并发只会让每个任务都变慢"
    assert EMBEDDER.max_jobs >= 8, "IO 道并发太小，等回包的时间完全没有重叠"


def test_scaling_notes_are_present_and_distinct() -> None:
    """`scaling` 会打进启动日志 —— 运维调副本数时最先看到的就是它。
    两条道复制粘贴同一句，等于没写。"""
    notes = {p.scaling for p in PROFILES}
    assert all(notes), "有 profile 没写扩容说明"
    assert len(notes) == len(PROFILES), "两条道的扩容说明是同一句"


def test_every_lane_the_ingest_pipeline_uses_has_a_worker() -> None:
    """漏掉一条道的后果：任务静静停在没人消费的队列上，PENDING、无错、无日志。

    这是分道设计里最难排查的失效模式（见
    `tests/integration/test_workers_split.py::test_task_stalls_...`），
    所以在**加阶段的那一刻**就拦下来，而不是等上线后。
    """
    runner = IngestRunner(
        embedding_model=None,  # type: ignore[arg-type] —— _build_flow 用不到它们
        vector_store=None,  # type: ignore[arg-type]
        knowledge_base=None,  # type: ignore[arg-type]
    )
    declared = {lane for _, _, lane in runner._flow.stages if lane is not None}  # noqa: SLF001
    served = {p.lane for p in PROFILES}

    assert declared, "入库流水线一条道都没声明 —— 分道会被整体跳过"
    assert declared <= served, f"这些道没有 worker 服务：{sorted(declared - served)}"
    assert declared <= set(LANE_QUEUES), (
        f"这些道没有对应队列：{sorted(declared - set(LANE_QUEUES))}"
    )


def test_lease_is_far_larger_than_the_heartbeat_interval() -> None:
    """租约取得太紧 = 误判活着的 worker 已死 = 一份任务两个执行者。

    心跳每 `TaskContext.heartbeat_interval` 一次（默认 10s），租约必须留出
    好几倍余量 —— 一次 GC 停顿、一段慢查询都可能让心跳迟到。
    """
    beat = TaskContext(None, "x")._hb_interval  # type: ignore[arg-type]  # noqa: SLF001
    assert beat * 5 <= DEFAULT_LEASE, (
        f"租约 {DEFAULT_LEASE} 相对心跳间隔 {beat} 太紧，会误收还活着的 worker"
    )


def test_workers_carry_the_sweep_cron_and_it_is_unique() -> None:
    """回收定时器挂在每个 worker 上；靠 arq 的 unique 保证多副本下只跑一份。

    `unique=False` 会让 N 个副本同时回收同一批任务 —— 那正是回收本身要防的
    "多个执行者"，只是搬到了回收器上。
    """
    job = sweep_cron()
    assert job.unique is True
    assert job.run_at_startup is True, "集群刚起来时该先扫一遍上一轮的僵尸"
    assert all(p.sweep for p in PROFILES), "有 worker 没挂回收定时器"

    for profile in PROFILES:
        settings = build_settings(profile, config=_dummy_config())
        names = [cj.name for cj in settings.cron_jobs]
        assert names == ["sweep_stale_tasks"], f"{profile.name} 的 cron 是 {names}"


def _dummy_config() -> APPConfig:
    """只为让 `build_settings` 能拼出 RedisSettings，不连任何东西。"""
    return APPConfig(
        server_config=ServerConfig(app_name="t", host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub", dim=4
            ),
            redis=RedisConfig(host="localhost", port=6379),
        ),
    )


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_job_timeout_is_generous_enough_for_a_real_document(profile) -> None:
    """超时短于真实耗时的话，任务会在半路被 arq 掐掉，
    表现为"莫名其妙的取消"，且只在大文档上出现。"""
    assert profile.job_timeout >= 600, f"{profile.name} 的 job_timeout 太短"
