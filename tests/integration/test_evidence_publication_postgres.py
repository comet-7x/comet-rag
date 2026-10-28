from __future__ import annotations

import os

import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.evidence.s3 import AsyncS3EvidenceStore, S3EvidenceError
from comet_rag.infrastructure.persistence.sql import Database
from comet_rag.infrastructure.persistence.task_store.postgres import PostgresTaskStore
from comet_rag.services.evidence_publication import (
    PERMISSION,
    PUBLICATION_PERMISSION_KIND,
    verify_publication_target,
)
from comet_rag.services.task_retention import guard_task_deletion
from comet_rag.tasks import TaskBusy
from tests.integration.conftest import truncate_tables
from tests.integration.test_s3_evidence_minio import scope as scope

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("fault", ["claim_reply", "verified_reply", "put_reply"])
async def test_permission_survives_connection_restart_without_blind_probe_retry(
    scope, database, postgres_dsn, monkeypatch, fault
):
    if os.environ.get("COMET_TEST_S3_CONDITIONAL") != "1":
        pytest.skip("需显式使用已验收条件写的独立S3命名空间")
    tracked, publisher, bucket = scope
    await truncate_tables(database, "tasks")
    tasks = PostgresTaskStore(database)
    original = tasks._put_context
    native_put = tracked.put_object

    async def lose_checkpoint(task_id, values, **kwargs):
        result = await original(task_id, values, **kwargs)
        state = values[PERMISSION]["state"]
        if (fault == "claim_reply" and state == "probing") or (
            fault == "verified_reply" and state == "verified"
        ):
            raise OSError("injected committed SQL response loss")
        return result

    async def lose_put(**kwargs):
        result = await native_put(**kwargs)
        if kwargs["Key"] == publisher.probe_key:
            raise OSError("injected committed PUT response loss")
        return result

    monkeypatch.setattr(tasks, "_put_context", lose_checkpoint)
    if fault == "put_reply":
        monkeypatch.setattr(tracked, "put_object", lose_put)
    with pytest.raises((OSError, S3EvidenceError)):
        await verify_publication_target(tasks, publisher, "documents_v1")
    monkeypatch.setattr(tracked, "put_object", native_put)
    before = list(tracked.puts)
    other = Database(postgres_dsn)
    try:
        fresh = PostgresTaskStore(other)
        (record,) = await fresh.list_tasks(kind=PUBLICATION_PERMISSION_KIND)
        assert record.request["probe_key"] == publisher.probe_key
        if fault == "verified_reply":
            assert record.context == {PERMISSION: {"state": "verified"}}
            await verify_publication_target(fresh, publisher, "documents_v1")
        else:
            assert record.context[PERMISSION]["state"] == "probing"
            with pytest.raises(TaskBusy):
                await verify_publication_target(fresh, publisher, "documents_v1")
        assert tracked.puts == before
        with pytest.raises(TaskBusy):
            await fresh.delete(record.task_id, force=True, guard=guard_task_deletion)
        # 新SDK用新key重新验收，旧未知对象及身份继续保留。
        reopened = AsyncS3EvidenceStore(
            client=tracked, bucket=bucket, gate=Gate(limit=1, max_waiting=2)
        )
        await verify_publication_target(fresh, reopened, "documents_v1")
        assert len(await fresh.list_tasks(kind=PUBLICATION_PERMISSION_KIND)) == 2
    finally:
        await other.close()
