from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from comet_rag.core.concurrency import Gate
from evals.pdf.s3_evidence import AsyncS3EvidenceStore, S3EvidenceError
from tests.fixtures.minio import MinioCredentials
from tests.unit.test_pdf_evidence_bundle import packet

pytestmark = pytest.mark.integration


class TrackedClient:
    """所有PUT前登记精确key；故障只发生在本测试拥有的新桶。"""

    def __init__(self, client):
        self.client = client
        self.meta = client.meta
        self.keys: set[str] = set()
        self.lose_manifest = False
        self.pause_put: str | None = None
        self.pause_read = False
        self.entered = asyncio.Event()
        self.closed_bodies = 0
        self.puts: list[str] = []
        self.successful_puts: list[str] = []

    async def put_object(self, **kwargs):
        self.keys.add(kwargs["Key"])
        self.puts.append(kwargs["Key"])
        response = await self.client.put_object(**kwargs)
        self.successful_puts.append(kwargs["Key"])
        if kwargs["Key"] == self.pause_put:
            self.entered.set()
            await asyncio.Event().wait()
        if self.lose_manifest and kwargs["Key"].endswith("/manifest.json"):
            self.lose_manifest = False
            raise OSError("injected committed reply loss")
        return response

    async def get_object(self, **kwargs):
        response = await self.client.get_object(**kwargs)
        raw = response["Body"]
        owner = self

        class Body:
            async def read(self, size):
                if owner.pause_read:
                    owner.entered.set()
                    await asyncio.Event().wait()
                return await raw.read(size)

            def close(self):
                owner.closed_bodies += 1
                raw.close()

        response["Body"] = Body()
        return response


@pytest.fixture
async def scope(
    minio_endpoint: str,
    minio_credentials: MinioCredentials,
    request: pytest.FixtureRequest,
):
    aioboto3 = pytest.importorskip("aioboto3")
    config = Config(
        signature_version="s3v4",
        region_name="us-east-1",
        s3={"addressing_style": "path"},
        retries={"mode": "standard", "total_max_attempts": 1},
        connect_timeout=5,
        read_timeout=5,
        max_pool_connections=getattr(request, "param", 1),
        proxies={},
    )
    async with aioboto3.Session().client(
        "s3",
        endpoint_url=minio_endpoint,
        aws_access_key_id=minio_credentials.access_key,
        aws_secret_access_key=minio_credentials.secret_key,
        config=config,
    ) as client:
        bucket = "comet-evidence-integration-" + uuid4().hex
        await client.create_bucket(Bucket=bucket)
        tracked = TrackedClient(client)
        adapter = AsyncS3EvidenceStore(
            client=tracked,
            bucket=bucket,
            gate=Gate(limit=2, max_waiting=4, acquire_timeout=1),
        )
        try:
            yield tracked, adapter, bucket
        finally:
            for key in tracked.keys:
                await client.delete_object(Bucket=bucket, Key=key)
            await client.delete_bucket(Bucket=bucket)
            with pytest.raises(ClientError) as missing:
                await client.head_bucket(Bucket=bucket)
            assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


async def seed(client, bucket, bundle):
    # 只建立读测试fixture，不把这些无条件PUT称作不可变证据发布。
    for name, data in (
        ("structured_content.json", bundle.native),
        ("ledger.json", bundle.ledger),
        ("manifest.json", bundle.manifest),
    ):
        await client.put_object(
            Bucket=bucket,
            Key=bundle.reference.prefix + "/" + name,
            Body=data,
            ContentType="application/json",
        )


async def test_target_capability_gate_records_rejection_without_business_writes(
    scope, record_property
) -> None:
    client, adapter, _ = scope
    try:
        await adapter.verify_target()
    except S3EvidenceError:
        record_property("conditional_capability", "rejected")
        assert not adapter._verified
        with pytest.raises(S3EvidenceError, match="conditional_capability_unverified"):
            await adapter.publish(packet())
    else:
        record_property("conditional_capability", "accepted")
        assert adapter._verified
    assert all(k.startswith("evidence-capability/") for k in client.keys)


async def test_target_bounded_read_of_owned_fixture(scope) -> None:
    client, adapter, bucket = scope
    bundle = packet()
    await seed(client, bucket, bundle)
    result = await adapter.read(bundle.reference)
    assert result.canonical_sha256 == bundle.reference.canonical_sha256
    assert client.closed_bodies == 3


@pytest.mark.parametrize("fault", ["digest", "gzip", "missing"])
async def test_target_abnormal_objects_are_rejected(scope, fault: str) -> None:
    client, adapter, bucket = scope
    bundle = packet()
    await seed(client, bucket, bundle)
    key = bundle.reference.prefix + "/manifest.json"
    if fault == "missing":
        await client.client.delete_object(Bucket=bucket, Key=key)
    else:
        kwargs = {"ContentEncoding": "gzip"} if fault == "gzip" else {}
        await client.put_object(
            Bucket=bucket,
            Key=key,
            Body=bundle.manifest if fault == "gzip" else b"X" * len(bundle.manifest),
            ContentType="application/json",
            **kwargs,
        )
    with pytest.raises(S3EvidenceError):
        await adapter.read(bundle.reference)
    assert adapter._gate.stats.in_flight == 0


async def test_target_cancel_read_releases_single_connection_for_reuse(scope) -> None:
    client, adapter, bucket = scope
    bundle = packet()
    await seed(client, bucket, bundle)
    client.pause_read = True
    work = asyncio.create_task(adapter.read(bundle.reference))
    await asyncio.wait_for(client.entered.wait(), 2)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert client.closed_bodies == 1
    client.pause_read = False
    result = await asyncio.wait_for(adapter.read(bundle.reference), 2)
    assert result.canonical_sha256 == bundle.reference.canonical_sha256


async def test_target_read_timeout_closes_native_body(scope) -> None:
    from evals.pdf.s3_evidence import S3EvidenceLimits

    client, adapter, bucket = scope
    bundle = packet()
    await seed(client, bucket, bundle)
    adapter._limits = S3EvidenceLimits(total_seconds=0.05)
    client.pause_read = True
    with pytest.raises(S3EvidenceError, match="deadline_exceeded"):
        await adapter.read(bundle.reference)
    assert client.closed_bodies == 1
    client.pause_read = False
    adapter._limits = S3EvidenceLimits()
    assert (
        await adapter.read(bundle.reference)
    ).canonical_sha256 == bundle.reference.canonical_sha256


def require_conditionals() -> None:
    if os.environ.get("COMET_TEST_S3_CONDITIONAL") != "1":
        pytest.skip(
            "原生条件发布是独立验收；显式设置COMET_TEST_S3_CONDITIONAL=1才运行，能力不符将fail"
        )


@pytest.mark.parametrize("scope", [2], indirect=True)
async def test_required_conditional_duplicate_and_concurrent_publication(scope) -> None:
    require_conditionals()
    client, adapter, _ = scope
    await adapter.verify_target()
    bundle = packet()
    results = await asyncio.gather(adapter.publish(bundle), adapter.publish(bundle))
    assert all(r.canonical_sha256 == bundle.reference.canonical_sha256 for r in results)
    assert await adapter.read(bundle.reference)
    assert (
        len(
            [
                key
                for key in client.successful_puts
                if key.startswith(bundle.reference.prefix)
            ]
        )
        == 3
    )


async def test_required_conditional_lost_manifest_reply_recovers_by_exact_read(
    scope,
) -> None:
    require_conditionals()
    client, adapter, _ = scope
    await adapter.verify_target()
    bundle = packet()
    client.lose_manifest = True
    with pytest.raises(S3EvidenceError, match="put_response_unknown"):
        await adapter.publish(bundle)
    assert sum(k.endswith("/manifest.json") for k in client.puts) == 1
    result = await adapter.publish(bundle)
    assert result.canonical_sha256 == bundle.reference.canonical_sha256


async def test_required_conditional_cancel_after_remote_commit_does_not_delete(
    scope,
) -> None:
    require_conditionals()
    client, adapter, bucket = scope
    await adapter.verify_target()
    bundle = packet()
    client.pause_put = bundle.reference.prefix + "/structured_content.json"
    work = asyncio.create_task(adapter.publish(bundle))
    await asyncio.wait_for(client.entered.wait(), 2)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert (await client.client.head_object(Bucket=bucket, Key=client.pause_put))[
        "ContentLength"
    ] == len(bundle.native)
    client.pause_put = None
    assert (
        await adapter.publish(bundle)
    ).canonical_sha256 == bundle.reference.canonical_sha256


async def test_required_conditional_timeout_after_commit_preserves_unknown_key(
    scope,
) -> None:
    from evals.pdf.s3_evidence import S3EvidenceLimits

    require_conditionals()
    client, adapter, _ = scope
    await adapter.verify_target()
    bundle = packet()
    client.pause_put = bundle.reference.prefix + "/structured_content.json"
    adapter._limits = S3EvidenceLimits(total_seconds=0.05)
    with pytest.raises(S3EvidenceError) as timeout:
        await adapter.publish(bundle)
    assert timeout.value.code == "deadline_exceeded"
    assert timeout.value.uncertain_key == client.pause_put
    client.pause_put = None
    adapter._limits = S3EvidenceLimits()
    assert (
        await adapter.publish(bundle)
    ).canonical_sha256 == bundle.reference.canonical_sha256
