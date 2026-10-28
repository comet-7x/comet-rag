from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from comet_rag.core.concurrency import Gate
from evals.pdf.s3_evidence import (
    AsyncS3EvidenceStore,
    S3EvidenceError,
    S3EvidenceLimits,
)
from tests.unit.test_pdf_evidence_bundle import packet


def rejected(status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": str(status)},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )


class Body:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.offset = 0
        self.closed = False
        self.reads = 0
        self.block: asyncio.Event | None = None

    async def read(self, size: int) -> bytes:
        assert size == 64 * 1024
        self.reads += 1
        if self.block is not None:
            await self.block.wait()
        result = self.raw[self.offset : self.offset + size]
        self.offset += len(result)
        return result

    def close(self) -> None:
        self.closed = True


class Client:
    """手写原子模型只供故障注入，真实兼容性另用目标服务核验。"""

    def __init__(self) -> None:
        self.meta = SimpleNamespace(
            config=SimpleNamespace(retries={"total_max_attempts": 1})
        )
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.writes: list[str] = []
        self.bodies: list[Body] = []
        self.old_conditionals = False
        self.lose_manifest = False
        self.hang_put: str | None = None
        self.entered = asyncio.Event()
        self.put_commits_before_hang = False
        self.alter = lambda key, response: response

    async def put_object(self, **kwargs):
        key = kwargs["Key"]
        self.calls.append(("put", key))
        assert kwargs["IfNoneMatch"] == "*"
        assert "ChecksumSHA256" in kwargs
        assert (
            kwargs["ChecksumSHA256"]
            == base64.b64encode(sha256(kwargs["Body"]).digest()).decode()
        )
        if self.hang_put == key and not self.put_commits_before_hang:
            self.entered.set()
            await asyncio.Event().wait()
        if self.old_conditionals:
            if key not in self.objects:
                raise rejected(404)
        elif key in self.objects:
            raise rejected(412)
        self.objects[key] = kwargs["Body"]
        self.writes.append(key)
        if self.hang_put == key:
            self.entered.set()
            await asyncio.Event().wait()
        if self.lose_manifest and key.endswith("/manifest.json"):
            self.lose_manifest = False
            raise OSError("remote committed, response lost with private URL")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    async def get_object(self, **kwargs):
        key = kwargs["Key"]
        self.calls.append(("get", key))
        if key not in self.objects:
            raise rejected(404)
        body = Body(self.objects[key])
        self.bodies.append(body)
        return self.alter(
            key,
            {
                "Body": body,
                "ContentLength": len(body.raw),
                "ContentType": "application/json",
                "ResponseMetadata": {"HTTPStatusCode": 200},
            },
        )


def store(client: Client, **limits):
    return AsyncS3EvidenceStore(
        client=client,
        bucket="isolated",
        gate=Gate(limit=2, max_waiting=4, acquire_timeout=1),
        limits=S3EvidenceLimits(**limits),
    )


def seed(client, bundle):
    for name, data in (
        ("structured_content.json", bundle.native),
        ("ledger.json", bundle.ledger),
        ("manifest.json", bundle.manifest),
    ):
        client.objects[bundle.reference.prefix + "/" + name] = data


async def test_duplicate_and_concurrent_publication_never_rewrite_objects() -> None:
    client = Client()
    adapter = store(client)
    await adapter.verify_target()
    bundle = packet()
    first, second = await asyncio.gather(
        adapter.publish(bundle), adapter.publish(bundle)
    )
    assert (
        first.canonical_sha256
        == second.canonical_sha256
        == bundle.reference.canonical_sha256
    )
    assert len([k for k in client.writes if k.startswith(bundle.reference.prefix)]) == 3
    assert all(b.closed for b in client.bodies)
    assert adapter._gate.stats.in_flight == 0


async def test_lost_manifest_reply_is_uncertain_and_explicit_retry_only_reads_matches() -> (
    None
):
    client = Client()
    adapter = store(client)
    await adapter.verify_target()
    bundle = packet()
    client.lose_manifest = True
    with pytest.raises(S3EvidenceError) as lost:
        await adapter.publish(bundle)
    assert lost.value.code == "put_response_unknown"
    assert lost.value.uncertain_key == bundle.reference.prefix + "/manifest.json"
    assert "private URL" not in str(lost.value)
    assert (
        sum(k.endswith("/manifest.json") for kind, k in client.calls if kind == "put")
        == 1
    )
    result = await adapter.publish(bundle)
    assert result.canonical_sha256 == bundle.reference.canonical_sha256
    assert len([k for k in client.writes if k.startswith(bundle.reference.prefix)]) == 3


async def test_unsafe_target_is_rejected_before_evidence_writes() -> None:
    client = Client()
    client.old_conditionals = True
    adapter = store(client)
    with pytest.raises(S3EvidenceError, match="conditional_put_rejected"):
        await adapter.verify_target()
    with pytest.raises(S3EvidenceError, match="conditional_capability_unverified"):
        await adapter.publish(packet())
    assert all(not k.startswith("evidence/v1/") for _, k in client.calls)


async def test_target_that_silently_overwrites_existing_probe_is_rejected() -> None:
    client = Client()
    adapter = store(client)
    original = client.put_object

    async def broken(**kwargs):
        if kwargs["Key"] in client.objects:
            client.objects.pop(kwargs["Key"])
        return await original(**kwargs)

    client.put_object = broken
    with pytest.raises(S3EvidenceError, match="conditional_overwrite_accepted"):
        await adapter.verify_target()
    assert not adapter._verified


@pytest.mark.parametrize("status", [400, 403, 404, 409, 500])
async def test_other_put_errors_do_not_authorize_unconditional_fallback(
    status: int,
) -> None:
    client = Client()
    adapter = store(client)
    await adapter.verify_target()

    async def broken(**kwargs):
        raise rejected(status)

    client.put_object = broken
    with pytest.raises(S3EvidenceError, match="conditional_put_rejected"):
        await adapter.publish(packet())


@pytest.mark.parametrize("committed", [False, True])
async def test_total_timeout_during_put_preserves_uncertainty(committed: bool) -> None:
    client = Client()
    adapter = store(client, total_seconds=0.04, request_seconds=1)
    await adapter.verify_target()
    bundle = packet()
    key = bundle.reference.prefix + "/structured_content.json"
    client.hang_put = key
    client.put_commits_before_hang = committed
    with pytest.raises(S3EvidenceError) as failure:
        await adapter.publish(bundle)
    assert failure.value.code == "deadline_exceeded"
    assert failure.value.uncertain_key == key
    assert (key in client.objects) == committed
    assert not any(k.endswith("/manifest.json") for k in client.objects)
    assert adapter._gate.stats.in_flight == 0


async def test_request_timeout_is_not_automatically_retried() -> None:
    client = Client()
    adapter = store(client, total_seconds=1, request_seconds=0.03)
    await adapter.verify_target()
    bundle = packet()
    client.hang_put = bundle.reference.prefix + "/structured_content.json"
    with pytest.raises(S3EvidenceError, match="put_response_unknown"):
        await adapter.publish(bundle)
    assert (
        sum(key == client.hang_put for kind, key in client.calls if kind == "put") == 1
    )


async def test_caller_cancel_after_put_commit_does_not_remove_shared_remote_bytes() -> (
    None
):
    client = Client()
    adapter = store(client)
    await adapter.verify_target()
    bundle = packet()
    client.hang_put = bundle.reference.prefix + "/structured_content.json"
    client.put_commits_before_hang = True
    work = asyncio.create_task(adapter.publish(bundle))
    await asyncio.wait_for(client.entered.wait(), 1)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert client.objects[client.hang_put] == bundle.native
    assert adapter._gate.stats.in_flight == 0


@pytest.mark.parametrize(
    "fault",
    [
        "gzip",
        "media",
        "bool_length",
        "huge_length",
        "short",
        "digest",
        "overflow",
        "status",
        "missing",
    ],
)
async def test_abnormal_objects_fail_and_bodies_close(fault: str) -> None:
    client = Client()
    adapter = store(client)
    bundle = packet()
    seed(client, bundle)
    key = bundle.reference.prefix + "/manifest.json"
    if fault == "missing":
        del client.objects[key]

    def alter(key, response):
        if fault == "gzip":
            response["ContentEncoding"] = "gzip"
        elif fault == "media":
            response["ContentType"] = "text/html"
        elif fault == "bool_length":
            response["ContentLength"] = True
        elif fault == "huge_length":
            response["ContentLength"] = 65537
        elif fault == "status":
            response["ResponseMetadata"]["HTTPStatusCode"] = 206
        elif fault == "short":
            response["Body"].raw = response["Body"].raw[:-1]
        elif fault == "digest":
            response["Body"].raw = b"X" * response["ContentLength"]
        elif fault == "overflow":
            response["Body"].raw += b"extra"
        return response

    client.alter = alter
    with pytest.raises(S3EvidenceError) as error:
        await adapter.read(bundle.reference)
    if fault == "overflow":
        assert error.value.code == "actual_receive_limit"
    elif fault in {"short", "digest"}:
        assert error.value.code == "object_digest_or_length_mismatch"
    assert all(b.closed for b in client.bodies)
    if fault in {"gzip", "media", "bool_length", "huge_length", "status"}:
        assert all(b.reads == 0 for b in client.bodies)
    assert adapter._gate.stats.in_flight == 0


async def test_cumulative_receive_budget_is_shared_across_bundle_objects() -> None:
    client = Client()
    bundle = packet()
    seed(client, bundle)
    adapter = store(client, received_bytes=len(bundle.manifest) + 1)
    with pytest.raises(S3EvidenceError, match="cumulative_receive_limit"):
        await adapter.read(bundle.reference)
    assert len(client.bodies) == 2 and all(b.closed for b in client.bodies)


async def test_read_cancel_closes_body_and_releases_gate() -> None:
    client = Client()
    bundle = packet()
    seed(client, bundle)
    adapter = store(client)
    entered = asyncio.Event()

    def alter(key, response):
        response["Body"].block = asyncio.Event()
        entered.set()
        return response

    client.alter = alter
    work = asyncio.create_task(adapter.read(bundle.reference))
    await asyncio.wait_for(entered.wait(), 1)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert client.bodies[0].closed
    assert adapter._gate.stats.in_flight == 0
    client.alter = lambda key, response: response
    assert (
        await adapter.read(bundle.reference)
    ).canonical_sha256 == bundle.reference.canonical_sha256


async def test_queue_wait_consumes_total_deadline_without_network() -> None:
    client = Client()
    adapter = store(client, total_seconds=0.03)
    adapter._gate = Gate(limit=1, max_waiting=1, acquire_timeout=1)
    await adapter._gate.acquire()
    try:
        with pytest.raises(S3EvidenceError, match="deadline_exceeded"):
            await adapter.read(packet().reference)
        assert not client.calls
    finally:
        adapter._gate.release()


async def test_existing_conflicting_object_is_not_repaired_or_overwritten() -> None:
    client = Client()
    adapter = store(client)
    await adapter.verify_target()
    bundle = packet()
    key = bundle.reference.prefix + "/structured_content.json"
    client.objects[key] = b"X" * len(bundle.native)
    with pytest.raises(S3EvidenceError, match="object_digest_or_length_mismatch"):
        await adapter.publish(bundle)
    assert client.objects[key] != bundle.native
    assert not any(k.endswith("/manifest.json") for k in client.objects)


async def test_invalid_local_bundle_is_rejected_before_upload() -> None:
    client = Client()
    adapter = store(client)
    await adapter.verify_target()
    client.calls.clear()
    with pytest.raises(S3EvidenceError):
        await adapter.publish(replace(packet(), native=b"{}"))
    assert not client.calls


@pytest.mark.parametrize("attempts", [None, 2])
def test_sdk_automatic_retries_are_rejected(attempts) -> None:
    client = Client()
    client.meta.config.retries = {"total_max_attempts": attempts}
    with pytest.raises(ValueError, match="关闭SDK"):
        store(client)


@pytest.mark.parametrize("bad_digest", [False, True])
async def test_body_close_failure_cannot_publish_success_or_mask_digest_error(
    bad_digest: bool,
) -> None:
    client = Client()
    bundle = packet()
    seed(client, bundle)
    adapter = store(client)

    def alter(key, response):
        body = response["Body"]
        if bad_digest:
            body.raw = b"X" * len(body.raw)

        def fail_close():
            body.closed = True
            raise OSError("injected body close")

        body.close = fail_close
        return response

    client.alter = alter
    with pytest.raises(S3EvidenceError) as error:
        await adapter.read(bundle.reference)
    assert error.value.code == (
        "object_digest_or_length_mismatch" if bad_digest else "body_close_failed"
    )
    if bad_digest:
        assert "连接回收未确认" in " ".join(error.value.__notes__)
    assert client.bodies[0].closed
    assert adapter._gate.stats.in_flight == 0


async def test_manifest_and_payload_reads_share_one_deadline_without_reset() -> None:
    client = Client()
    bundle = packet()
    seed(client, bundle)
    now = [0.0]
    adapter = AsyncS3EvidenceStore(
        client=client,
        bucket="isolated",
        gate=Gate(limit=1, max_waiting=1, acquire_timeout=1),
        limits=S3EvidenceLimits(total_seconds=1),
        clock=lambda: now[0],
    )

    def consume(key, response):
        now[0] += 0.4
        return response

    client.alter = consume
    with pytest.raises(S3EvidenceError, match="deadline_exceeded"):
        await adapter.read(bundle.reference)
    assert len(client.bodies) == 3
    assert all(b.closed for b in client.bodies)


async def test_get_request_timeout_is_distinct_from_total_deadline() -> None:
    client = Client()
    bundle = packet()
    seed(client, bundle)
    adapter = store(client, total_seconds=1, request_seconds=0.03)

    def pause(key, response):
        response["Body"].block = asyncio.Event()
        return response

    client.alter = pause
    with pytest.raises(S3EvidenceError, match="request_timeout"):
        await adapter.read(bundle.reference)
    assert client.bodies[0].closed


@pytest.mark.parametrize("error_type", [ReadTimeoutError, ConnectTimeoutError])
async def test_native_sdk_get_timeouts_are_classified_without_endpoint_leak(
    error_type,
) -> None:
    client = Client()

    async def timeout(**kwargs):
        raise error_type(endpoint_url="https://private-endpoint.invalid")

    client.get_object = timeout
    with pytest.raises(S3EvidenceError) as failure:
        await store(client).read(packet().reference)
    assert failure.value.code == "request_timeout"
    assert "private-endpoint" not in str(failure.value)
