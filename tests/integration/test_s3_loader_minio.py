"""S3Loader against the MinIO service from docker-compose.yml."""

from __future__ import annotations

import pytest

from comet_rag.engines.offload import run_sync
from comet_rag.infrastructure.sources.s3 import AsyncS3Loader, S3Loader
from tests.fixtures.minio import MinioCredentials, isolated_object
from tests.integration import conftest as integration_fixtures

pytestmark = pytest.mark.integration


def test_minio_fixture_probes_configured_endpoint(monkeypatch) -> None:
    endpoint = "https://minio.example.test:9443"
    calls: list[tuple[str, int, str]] = []
    monkeypatch.setattr(integration_fixtures, "MINIO_ENDPOINT", endpoint)
    monkeypatch.setattr(
        integration_fixtures,
        "_require",
        lambda host, port, name: calls.append((host, port, name)),
    )

    integration_fixtures._require_endpoint(endpoint, "MinIO")

    assert calls == [("minio.example.test", 9443, "MinIO")]


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://minio.example.test", ("minio.example.test", 80)),
        ("https://[2001:db8::1]", ("2001:db8::1", 443)),
    ],
)
def test_minio_endpoint_address_uses_scheme_default_port(
    endpoint: str, expected: tuple[str, int]
) -> None:
    assert integration_fixtures._endpoint_address(endpoint) == expected


@pytest.mark.parametrize("endpoint", ["localhost:9010", "ftp://minio.example.test"])
def test_minio_endpoint_address_rejects_malformed_urls(endpoint: str) -> None:
    with pytest.raises(ValueError, match="http/https"):
        integration_fixtures._endpoint_address(endpoint)


def test_minio_endpoint_address_rejects_port_zero() -> None:
    with pytest.raises(ValueError, match=r"1\.\.65535"):
        integration_fixtures._endpoint_address("http://minio.example.test:0")


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_s3_loader_round_trip_against_minio(
    minio_endpoint: str, minio_credentials: MinioCredentials, asynchronous: bool
) -> None:
    aioboto3 = pytest.importorskip("aioboto3", reason="需要 server extra")
    from botocore.exceptions import ClientError

    payload = b"integration object from minio"
    client_kwargs = {
        "endpoint_url": minio_endpoint,
        "aws_access_key_id": minio_credentials.access_key,
        "aws_secret_access_key": minio_credentials.secret_key,
        "region_name": "us-east-1",
    }

    async with aioboto3.Session().client("s3", **client_kwargs) as client:
        async with isolated_object(client, payload) as (bucket, key):
            loader = (AsyncS3Loader if asynchronous else S3Loader)(
                endpoint_url=minio_endpoint,
                access_key_id=minio_credentials.access_key,
                secret_access_key=minio_credentials.secret_key,
            )
            try:
                source = f"s3://{bucket}/{key}"
                content = (
                    await loader.load(source)
                    if isinstance(loader, AsyncS3Loader)
                    else await run_sync(loader.load, source)
                )
                assert content.path.read_bytes() == payload
                assert content.metadata["bucket"] == bucket
                assert content.metadata["object_key"] == key
                assert content.metadata["file_type"] == "txt"
            finally:
                if isinstance(loader, AsyncS3Loader):
                    await loader.cleanup()
                else:
                    await run_sync(loader.cleanup)
            assert not content.path.exists()
            assert loader._client is None

        with pytest.raises(ClientError) as missing:
            await client.head_bucket(Bucket=bucket)
        assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
