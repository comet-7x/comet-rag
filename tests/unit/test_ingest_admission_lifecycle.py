from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest

from comet_rag.api.routes.ingest import submit_ingest
from comet_rag.api.schemas.ingest import IngestAccepted, IngestSubmit
from comet_rag.services.source_policy import SourceNotAllowed, SourcePolicy
from comet_rag.tasks import InMemoryTaskStore, TaskExecutor, TaskService


class RecordingExecutor(TaskExecutor):
    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit(self, task_id: str, *, delay: float = 0.0) -> None:
        self.submitted.append(task_id)

    async def request_cancel(self, task_id: str) -> bool:
        return False

    async def shutdown(self, *, timeout: float | None = 10.0) -> None:
        pass


class BlockingDNS:
    def __init__(self, address: str = "93.184.216.34") -> None:
        self.address = address
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.hosts: list[str] = []
        self.thread_id: int | None = None

    def getaddrinfo(self, host: str, port: None) -> list[tuple[object, ...]]:
        self.hosts.append(host)
        self.thread_id = threading.get_ident()
        self.entered.set()
        try:
            assert self.release.wait(1), "test did not release DNS resolution"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.address, 0))]
        finally:
            self.finished.set()


def _submit(tasks: TaskService) -> asyncio.Task[IngestAccepted]:
    return asyncio.create_task(
        submit_ingest(
            IngestSubmit(kb_id="kb", source="https://documents.example/file.pdf"),
            tasks,
            SourcePolicy(),
            None,
            SimpleNamespace(
                ingest_kind="ingest", revision_ingest_binding=None, mineru_workflow=None
            ),  # type: ignore[arg-type]
        )
    )


async def test_ingest_dns_admission_leaves_the_event_loop_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns = BlockingDNS()
    monkeypatch.setattr(socket, "getaddrinfo", dns.getaddrinfo)
    store = InMemoryTaskStore()
    executor = RecordingExecutor()
    task = _submit(TaskService(store, executor))
    try:
        assert await asyncio.to_thread(dns.entered.wait, 1)
        progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(progressed.set)
        await asyncio.wait_for(progressed.wait(), timeout=0.2)
        assert not dns.finished.is_set(), (
            "event loop only progressed after blocking DNS returned"
        )
        assert dns.thread_id != threading.get_ident()
        assert not task.done()
        assert await store.list_tasks() == []
        assert executor.submitted == []
    finally:
        dns.release.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]

    assert isinstance(outcome, IngestAccepted)
    assert dns.hosts == ["documents.example"]
    assert dns.finished.is_set()
    assert executor.submitted == [outcome.task_id]
    assert len(await store.list_tasks()) == 1


@pytest.mark.parametrize(
    ("cancellations", "address"),
    [(1, "93.184.216.34"), (2, "93.184.216.34"), (2, "127.0.0.1")],
)
async def test_cancelled_admission_drains_dns_without_creating_a_task(
    cancellations: int,
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns = BlockingDNS(address)
    monkeypatch.setattr(socket, "getaddrinfo", dns.getaddrinfo)
    store = InMemoryTaskStore()
    executor = RecordingExecutor()
    task = _submit(TaskService(store, executor))
    try:
        assert await asyncio.to_thread(dns.entered.wait, 1)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        _, pending = await asyncio.wait({task}, timeout=0.02)
        assert task in pending, (
            "cancelled admission returned while DNS was still running"
        )
        assert not dns.finished.is_set()
        assert await store.list_tasks() == []
        assert executor.submitted == []
    finally:
        dns.release.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]

    assert isinstance(outcome, asyncio.CancelledError)
    assert dns.finished.is_set()
    assert await store.list_tasks() == []
    assert executor.submitted == []


async def test_dns_denial_is_propagated_without_creating_a_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns = BlockingDNS("127.0.0.1")
    dns.release.set()
    monkeypatch.setattr(socket, "getaddrinfo", dns.getaddrinfo)
    store = InMemoryTaskStore()
    executor = RecordingExecutor()

    with pytest.raises(SourceNotAllowed, match="非公网"):
        await _submit(TaskService(store, executor))

    assert dns.hosts == ["documents.example"]
    assert await store.list_tasks() == []
    assert executor.submitted == []
