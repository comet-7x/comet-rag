"""崩溃恢复：`kill -9` 掉 worker，任务被另一个 worker 接管跑完（T24）。

plan Checkpoint E 的第一条。**用真进程 + 真 SIGKILL**，不是在测试进程里
cancel 一个协程 —— 后者会走 `execute()` 的 `CancelledError` 分支，把任务
干净地落成 CANCELLED，恰恰绕开了本用例要验的那条路。

被杀的 worker 由 `crash_worker.py` 提供入口，跑的是 `arq` 命令行本身，
所以这条链路顺带也验了 CLI 入口能起来。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from arq.worker import Worker

from comet_rag.infrastructure.database import Database
from comet_rag.tasks import TaskStatus
from comet_rag.tasks.executor_arq import LANE_QUEUES, ArqExecutor, run_task
from comet_rag.tasks.store_postgres import PostgresTaskStore
from comet_rag.workers.maintenance import sweep_stale_tasks
from tests.contracts.support import wait_for_terminal, wait_until
from tests.integration.conftest import truncate_tables
from tests.integration.crash_worker import KIND

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: 与 test_e2e_workers 一致：本用例用生产队列名，靠独立 db 与开发机隔开
TEST_REDIS_DB = 14
#: 回收租约压到 1 秒。语义（"心跳超过 lease 就判死"）由断言覆盖，不靠真等 90 秒。
LEASE = timedelta(seconds=1)


def _write_config(directory: Path) -> Path:
    """给子进程写一份 config.yaml —— `get_config()` 从 cwd 读它。"""
    config = {
        "server_config": {"app_name": "crash-test", "host": "127.0.0.1", "port": 0},
        "infrastructure_config": {
            "embedding_model": {
                "base_url": "http://127.0.0.1:9/v1",  # 本用例的 runner 不碰模型
                "model_name": "stub-embed",
                "dim": 8,
            },
            "database": {
                "host": "localhost",
                "port": 5432,
                "username": "comet",
                "password": "comet",
                "database": "comet_rag",
            },
            "redis": {"host": "localhost", "port": 6379, "db_index": TEST_REDIS_DB},
        },
        # 向量库用 memory：本用例只验任务的生死接管，不该把 Milvus 也拉进来
        "backends": {
            "vector_store": "memory",
            "task_store": "postgres",
            "task_executor": "arq",
        },
    }
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
async def pool(redis_url: str) -> AsyncIterator[ArqRedis]:  # noqa: ARG001
    p = await create_pool(
        RedisSettings.from_dsn(f"redis://localhost:6379/{TEST_REDIS_DB}")
    )
    for queue in LANE_QUEUES.values():
        await p.delete(queue)
    try:
        yield p
    finally:
        await p.aclose()


@pytest.fixture
async def store(postgres_dsn: str) -> AsyncIterator[PostgresTaskStore]:
    await truncate_tables(postgres_dsn, "tasks")
    db = Database(postgres_dsn)
    try:
        yield PostgresTaskStore(db)
    finally:
        await db.aclose()
        await truncate_tables(postgres_dsn, "tasks")


@contextlib.contextmanager
def doomed_worker(tmp_path: Path):
    """起一个**真的** arq worker 子进程，退出时确保它死透。"""
    _write_config(tmp_path)
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(  # noqa: S603
        [
            str(PROJECT_ROOT / ".venv" / "bin" / "arq"),
            "tests.integration.crash_worker.WorkerSettings",
        ],
        cwd=tmp_path,  # get_config() 从 cwd 读 config.yaml
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def _make_taker(store: PostgresTaskStore, pool: ArqRedis) -> tuple[ArqExecutor, Worker]:
    """接管者：一个跑在测试进程里的普通 worker。"""
    executor = ArqExecutor(
        store,
        pool=pool,
        lanes=LANE_QUEUES,
        entry_lane="cpu",
        lane="cpu",
        retry_backoff=0.01,
    )
    worker = Worker(
        functions=[run_task],
        redis_pool=pool,
        queue_name=LANE_QUEUES["cpu"],
        max_jobs=2,
        poll_delay=0.02,
        handle_signals=False,
        ctx={"executor": executor},
        retry_jobs=False,
        max_tries=1,
        keep_result=2,
        log_results=False,
    )
    return executor, worker


async def test_killed_worker_task_is_reclaimed_and_finished_by_another(
    store: PostgresTaskStore, pool: ArqRedis, tmp_path: Path
) -> None:
    """完整一轮：跑起来 → `kill -9` → 卡住 → 回收 → 另一个 worker 跑完。"""
    producer = ArqExecutor(
        store, pool=pool, lanes=LANE_QUEUES, entry_lane="cpu", retry_backoff=0.01
    )
    task = await store.create(KIND, max_attempts=3)

    with doomed_worker(tmp_path) as proc:
        await producer.submit(task.task_id)
        await wait_until(
            lambda: _is(store, task.task_id, TaskStatus.RUNNING),
            timeout=60.0,
            message=f"子进程 worker 没把任务跑起来（exit={proc.poll()}）",
        )
        running = await store.require(task.task_id)
        assert running.worker_id, "RUNNING 却没记下是谁在跑，租约回收将无从判断"

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

    # 崩溃之后：没有人写过任何东西，任务就那么卡在 RUNNING
    await asyncio.sleep(0.3)
    zombie = await store.require(task.task_id)
    assert zombie.status is TaskStatus.RUNNING, (
        "worker 被 KILL 了却还是写下了终态 —— 那不是崩溃，用例没测到该测的东西"
    )
    assert zombie.error is None

    # 等租约过期，然后回收
    await asyncio.sleep(LEASE.total_seconds() + 0.2)
    taker, worker = _make_taker(store, pool)
    runner = asyncio.create_task(worker.async_run())
    try:
        requeued = await sweep_stale_tasks(
            {"context": _FakeContext(store), "executor": taker, "lease": LEASE}
        )
        assert requeued == 1

        done = await wait_for_terminal(store, task.task_id, timeout=30.0)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        for job in list(worker.tasks.values()):
            job.cancel()
        if worker.tasks:
            await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
        # 取消 job 会 detach 出写库的收尾协程，必须排干后才能让 `store` 夹具
        # 去关数据库 —— 否则连接会连着未结束的事务被拆掉，它持有的锁要等到
        # PostgreSQL 发现套接字断了才释放，期间隔壁文件的 TRUNCATE 会被卡住。
        await taker.shutdown(timeout=10.0)

    assert done.status is TaskStatus.SUCCEEDED, done
    assert done.attempts == 2, "接管应当是第二次尝试"
    assert done.result["worker"] == taker.worker_id, (
        "跑完它的不是接管者 —— 那第一个 worker 根本没死透"
    )
    assert done.worker_id == taker.worker_id
    assert running.worker_id != taker.worker_id, (
        "前后两次是同一个 worker，说明第一段根本不是那个子进程跑的"
    )


async def test_sweep_leaves_a_trail(store: PostgresTaskStore, pool: ArqRedis) -> None:
    """回收必须留痕：日志之外还要有 `TaskEvent`。

    没有留痕的话，用户只看到 attempts 平白多了 1，问"为什么我的任务重跑了"
    时谁也答不上来。
    """
    taker, _ = _make_taker(store, pool)
    task = await store.create(KIND, max_attempts=3)
    # 手工造一个僵尸：RUNNING + 心跳停在很久以前
    await store.transition(
        task.task_id, TaskStatus.RUNNING, attempts=1, worker_id="已经死了的 worker"
    )
    await store.update(task.task_id, heartbeat_at=_long_ago())

    await sweep_stale_tasks(
        {"context": _FakeContext(store), "executor": taker, "lease": LEASE}
    )

    revived = await store.require(task.task_id)
    assert revived.status is TaskStatus.PENDING
    assert revived.error is not None
    assert revived.error.code == "lease_expired"
    assert revived.error.retriable is True
    assert revived.worker_id is None, "回收后没清掉 worker_id，围栏会认错主人"

    notes = [e.message for e in await store.events(task.task_id)]
    assert any("租约过期" in n for n in notes), f"回收没留下事件：{notes}"


async def test_sweep_does_not_touch_live_tasks(
    store: PostgresTaskStore, pool: ArqRedis
) -> None:
    """心跳正常的任务一根汗毛都不能动 —— 误收就是"一份任务两个执行者"。"""
    taker, _ = _make_taker(store, pool)
    task = await store.create(KIND, max_attempts=3)
    await store.transition(
        task.task_id, TaskStatus.RUNNING, attempts=1, worker_id="活得好好的"
    )

    requeued = await sweep_stale_tasks(
        {"context": _FakeContext(store), "executor": taker, "lease": LEASE}
    )

    assert requeued == 0
    still = await store.require(task.task_id)
    assert still.status is TaskStatus.RUNNING
    assert still.worker_id == "活得好好的"


async def test_stale_worker_cannot_write_after_being_reclaimed(
    store: PostgresTaskStore, pool: ArqRedis
) -> None:
    """**围栏**：被回收后，原 worker 再想写终态必须一个字都写不进去。

    租约判死无法做到绝对准确 —— "没心跳"和"死了"本来就分不清。所以一定
    存在这种局面：原 worker 其实还活着，只是慢了。它跑完后若照样写，
    要么覆盖接管者的结果，要么（PENDING → SUCCEEDED 非法）把异常抛回
    `execute()`，被收口成 `_mark_failed`，**把一条正在被别人正常执行的任务
    判死**。后者尤其恶劣：任务明明成功了，最终却是 FAILED。
    """
    stale, _ = _make_taker(store, pool)
    task = await store.create(KIND, max_attempts=3)
    # 原 worker 认领
    await store.transition(
        task.task_id, TaskStatus.RUNNING, attempts=1, worker_id=stale.worker_id
    )
    # 回收 + 被别人接手
    await store.transition(task.task_id, TaskStatus.PENDING, worker_id=None)
    await store.transition(
        task.task_id, TaskStatus.RUNNING, attempts=2, worker_id="接管者"
    )

    # 原 worker 姗姗来迟地想收尾
    from comet_rag.tasks.runner import Done  # noqa: PLC0415

    await stale._finish(task.task_id, Done(result="迟到的结果"))  # noqa: SLF001
    await stale._mark_failed(task.task_id, RuntimeError("迟到的失败"))  # noqa: SLF001

    after = await store.require(task.task_id)
    assert after.status is TaskStatus.RUNNING, (
        f"原 worker 越过围栏改掉了状态：{after.status.value}"
    )
    assert after.worker_id == "接管者"
    assert after.result is None


class _FakeContext:
    """`sweep_stale_tasks` 只用到 `ctx["context"].task_store`。

    不搭一整个 `Context`：那会连带建起模型客户端与向量库连接，而本用例
    与它们毫无关系 —— 依赖越少，失败时的指向越准。
    """

    def __init__(self, store: PostgresTaskStore) -> None:
        self.task_store = store


def _long_ago():
    from comet_rag.core.time import Time  # noqa: PLC0415

    return Time.now() - timedelta(hours=1)


async def _is(store: PostgresTaskStore, task_id: str, status: TaskStatus) -> bool:
    task = await store.get(task_id)
    return task is not None and task.status is status


if sys.platform == "win32":  # pragma: no cover
    pytest.skip("SIGKILL 语义依赖 POSIX", allow_module_level=True)
