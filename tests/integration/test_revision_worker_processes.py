from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from comet_rag.core.time import Time
from comet_rag.services.revision_context import load_task_handoff
from comet_rag.tasks import TaskStatus
from tests.contracts.support import wait_until
from tests.integration import test_revision_ingestion_worker as revision_support

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.name != "posix", reason="原生 SIGTERM/SIGKILL 演练需要 POSIX"
    ),
]
ROOT = Path(__file__).resolve().parents[2]
deployment = revision_support.production  # 只共享 API/真实存储夹具，worker 均另起进程。


def secret(value):
    return value.get_secret_value() if isinstance(value, SecretStr) else value


class Child:
    def __init__(self, process: subprocess.Popen, directory: Path) -> None:
        self.process, self.directory = process, directory

    def events(self) -> list[dict]:
        path = self.directory / "events.jsonl"
        if not path.exists():
            return []
        text = path.read_text()
        return [json.loads(row) for row in text.splitlines() if row.endswith("}")]

    async def wait_event(self, name: str) -> dict:
        async def seen():
            if self.process.poll() is not None:
                raise AssertionError(
                    f"子进程提前退出 {self.process.returncode}；日志 {self.directory / 'worker.log'}"
                )
            return any(row["event"] == name for row in self.events())

        await wait_until(seen, timeout=20, message=f"子进程未报告 {name}")
        return next(row for row in self.events() if row["event"] == name)

    async def exited(self, expected: int = 0) -> None:
        await wait_until(self._exited, timeout=20, message="worker 未退出")
        assert self.process.returncode == expected, self.directory / "worker.log"

    async def _exited(self) -> bool:
        return self.process.poll() is not None

    async def dispose(self) -> None:
        if self.process.poll() is None:
            (self.directory / "release").touch()
            self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, 10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 10)
                raise AssertionError("测试 worker 未正常排干，已强制回收") from None
            assert self.process.returncode == 0, self.directory / "worker.log"


@pytest.fixture
def spawn(deployment, tmp_path):
    async def start(lane, *, barrier="none", recover=False, grace=None):
        directory = tmp_path / f"child-{len(list(tmp_path.glob('child-*')))}"
        directory.mkdir()
        raw = deployment.config.model_dump(mode="json")
        infrastructure = deployment.config.infrastructure_config
        assert infrastructure.database is not None
        raw["infrastructure_config"]["database"]["password"] = secret(
            infrastructure.database.password
        )
        assert (
            infrastructure.redis is not None
            and infrastructure.vector_database is not None
        )
        raw["infrastructure_config"]["redis"]["password"] = secret(
            infrastructure.redis.password
        )
        raw["infrastructure_config"]["vector_database"]["api_key"] = (
            infrastructure.vector_database.api_key_value
        )
        config = directory / "config.yaml"
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        config.chmod(0o600)
        command = [
            sys.executable,
            "-m",
            "tests.integration.revision_process_worker",
            "--config",
            str(config),
            "--directory",
            str(directory),
            "--lane",
            lane,
            "--barrier",
            barrier,
        ]
        if recover:
            command.append("--recover")
        if grace is not None:
            command.extend(["--grace", str(grace)])
        with (directory / "worker.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(  # noqa: S603 —— 固定测试入口及本夹具生成的参数
                command,
                cwd=directory,
                env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"},
                stdout=log,
                stderr=subprocess.STDOUT,
            )  # noqa: S603 —— 测试入口和参数由本夹具生成
        child = Child(process, directory)
        deployment.cleanup.push_async_callback(child.dispose)
        ready = await child.wait_event("ready")
        assert ready["pid"] == process.pid and process.pid != os.getpid()
        return child

    return start


async def prepare(deployment, spawn):
    cpu = await spawn("cpu")
    task_id = await deployment.submit()

    async def handed():
        task = await deployment.context.task_store.require(task_id)
        return task.status is TaskStatus.PENDING and task.resume_stage == "indexing"

    await wait_until(handed, timeout=20, message="CPU 进程未完成 handoff")
    cpu.process.send_signal(signal.SIGTERM)
    await cpu.exited()
    assert any(row["event"] == "closed" for row in cpu.events())
    assert not any(row["event"] == "embedding" for row in cpu.events())
    return task_id, cpu


async def test_sigterm_drains_revision_and_leaves_new_work_for_another_process(
    deployment, spawn
):
    task_id, cpu = await prepare(deployment, spawn)
    io = await spawn("io", barrier="before-activation")
    await io.wait_event("barrier")
    io.process.send_signal(signal.SIGTERM)
    await io.wait_event("stopped_picking")
    queued = await deployment.submit()
    (io.directory / "release").touch()
    await io.exited()
    done = await deployment.done(task_id)
    assert done.status is TaskStatus.SUCCEEDED
    assert (
        await deployment.context.task_store.require(queued)
    ).status is TaskStatus.PENDING
    assert [row["event"] for row in io.events()][-2:] == ["model_closed", "closed"]
    await deployment.visible(done.result["revision_id"])
    await spawn("cpu")
    other = await spawn("io")
    final = await deployment.done(queued)
    assert final.status is TaskStatus.SUCCEEDED and other.process.pid != io.process.pid
    assert cpu.process.pid != io.process.pid


@pytest.mark.parametrize("barrier", ["before-activation", "after-activation"])
async def test_sigkill_recovers_revision_from_persisted_phase_without_cpu_or_reembedding(
    deployment, spawn, barrier
):
    task_id, _ = await prepare(deployment, spawn)
    io = await spawn("io", barrier=barrier)
    marker = await io.wait_event("barrier")
    running = await deployment.context.task_store.require(task_id)
    draft = load_task_handoff(running.context, task_id).draft
    assert marker["revision"] == draft.key.revision_id
    io.process.kill()
    await io.exited(-signal.SIGKILL)
    assert not any(row["event"] == "closed" for row in io.events())
    zombie = await deployment.context.task_store.require(task_id)
    assert zombie.status is TaskStatus.RUNNING
    assert zombie.context["revision_publication"]["phase"] == "activating"
    if barrier == "after-activation":
        await deployment.visible(draft.key.revision_id)
    # 真正进程已死才推进故障时钟；沿用生产 90s 判死规则，不实际等待 90s。
    await deployment.context.task_store.update(
        task_id, heartbeat_at=Time.now() - timedelta(seconds=180)
    )
    deployment.path.unlink()
    recovered = await spawn("io", recover=True)
    done = await deployment.done(task_id)
    assert done.status is TaskStatus.SUCCEEDED and done.attempts == 2
    assert done.worker_id != running.worker_id
    assert done.result["revision_id"] == draft.key.revision_id
    assert not any(row["event"] == "embedding" for row in recovered.events())
    assert len([row for row in io.events() if row["event"] == "embedding"]) == 4
    manifest = await deployment.context.document_store.reserve_revision(draft)
    assert manifest.generation == 1 and manifest.status.value == "active"
    await deployment.visible(draft.key.revision_id)


async def test_expired_sigterm_grace_preserves_committed_active_and_cancel_semantics(
    deployment, spawn
):
    task_id, _ = await prepare(deployment, spawn)
    io = await spawn("io", barrier="after-activation", grace=1)
    await io.wait_event("barrier")
    before = await deployment.context.task_store.require(task_id)
    draft = load_task_handoff(before.context, task_id).draft
    io.process.send_signal(signal.SIGTERM)
    await io.exited()
    cancelled = await deployment.done(task_id)
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.context["revision_publication"]["phase"] == "activating"
    assert any(row["event"] == "closed" for row in io.events())
    await deployment.visible(draft.key.revision_id)
    # 保留现有契约：取消是终态，不能伪装成可自动重试的失败。
    response = await deployment.client.post(f"/tasks/{task_id}/retry")
    assert response.status_code == 400
    await spawn("cpu")
    await spawn("io")
    replacement = await deployment.done(await deployment.submit())
    assert replacement.status is TaskStatus.SUCCEEDED
    assert replacement.result["revision_id"] != draft.key.revision_id
    assert (
        await deployment.context.document_store.reserve_revision(draft)
    ).status.value == "purged"
    await deployment.visible(replacement.result["revision_id"])
