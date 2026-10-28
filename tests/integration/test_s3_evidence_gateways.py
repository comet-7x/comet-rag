from __future__ import annotations

import asyncio
import os

import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.evidence.s3 import AsyncS3EvidenceStore
from tests.integration.test_s3_evidence_minio import (
    TrackedClient,
)
from tests.integration.test_s3_evidence_minio import (
    scope as scope,
)
from tests.unit.test_pdf_evidence_bundle import packet

pytestmark = pytest.mark.integration


async def test_distinct_gateways_publish_one_bundle_and_preserve_existing_bytes(
    scope, minio_credentials
) -> None:
    endpoint = os.environ.get("COMET_TEST_S3_SECOND_ENDPOINT")
    if not endpoint:
        pytest.skip("跨gateway验收需要独立的第二端点，不用同端点两连接冒充")
    client, adapter, bucket = scope
    aioboto3 = pytest.importorskip("aioboto3")

    async with aioboto3.Session().client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=minio_credentials.access_key,
        aws_secret_access_key=minio_credentials.secret_key,
        config=client.meta.config,
    ) as raw:
        other = TrackedClient(raw)
        remote = AsyncS3EvidenceStore(
            client=other,
            bucket=bucket,
            gate=Gate(limit=2, max_waiting=4, acquire_timeout=1),
        )
        bundle = packet()
        try:
            await asyncio.gather(adapter.verify_target(), remote.verify_target())
            first, second = await asyncio.gather(
                adapter.publish(bundle), remote.publish(bundle)
            )
            assert (
                first.canonical_sha256
                == second.canonical_sha256
                == bundle.reference.canonical_sha256
            )
            evidence_puts = [
                k
                for k in client.successful_puts + other.successful_puts
                if k.startswith(bundle.reference.prefix)
            ]
            assert len(evidence_puts) == 3
            assert (await remote.read(bundle.reference)).document == first.document
        finally:
            # 同一新桶、两份PUT前登记清单；移交给scope的精确清理，不列举其他对象。
            client.keys.update(other.keys)
