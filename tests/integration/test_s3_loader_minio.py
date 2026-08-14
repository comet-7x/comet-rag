"""S3Loader against the MinIO service from docker-compose.yml."""

from __future__ import annotations

from uuid import uuid4

import pytest

from comet_rag.infrastructure.loaders import S3Loader

pytestmark = pytest.mark.integration


async def test_s3_loader_round_trip_against_minio(minio_endpoint: str) -> None:
    aioboto3 = pytest.importorskip("aioboto3", reason="需要 server extra")
    bucket = f"comet-rag-test-{uuid4().hex[:12]}"
    key = "documents/integration.txt"
    payload = b"integration object from minio"
    client_kwargs = {
        "endpoint_url": minio_endpoint,
        "aws_access_key_id": "minioadmin",
        "aws_secret_access_key": "minioadmin",
        "region_name": "us-east-1",
    }

    async with aioboto3.Session().client("s3", **client_kwargs) as client:
        await client.create_bucket(Bucket=bucket)
        await client.put_object(Bucket=bucket, Key=key, Body=payload)

        loader = S3Loader(
            endpoint_url=minio_endpoint,
            access_key_id="minioadmin",
            secret_access_key="minioadmin",  # noqa: S106 - local test credential
        )
        try:
            content = await loader.aload(f"s3://{bucket}/{key}")
            assert content.path.read_bytes() == payload
            assert content.metadata["bucket"] == bucket
            assert content.metadata["object_key"] == key
            assert content.metadata["file_type"] == "txt"
        finally:
            await loader.acleanup()
            await client.delete_object(Bucket=bucket, Key=key)
            await client.delete_bucket(Bucket=bucket)

        assert not content.path.exists()
        assert loader._async_client is None
