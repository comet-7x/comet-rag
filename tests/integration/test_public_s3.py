from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import boto3
import botocore.session
import httpx
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.credentials import CredentialResolver

from comet_rag.documents.parsers import AsyncTextParser
from comet_rag.documents.projection import project_document
from comet_rag.sources import (
    AsyncS3SourceResolver,
    S3Source,
    SourceResourceLimitExceeded,
)

pytestmark = pytest.mark.integration


def test_public_aws_s3_download_parse_metadata_and_cleanup(tmp_path: Path) -> None:
    # NOAA 的公开小型版本文件；只读，不查找本机 AWS 配置或凭据。
    session = botocore.session.Session()
    session.set_config_variable("config_file", "/dev/null")
    session.set_config_variable("credentials_file", "/dev/null")
    session.register_component("credential_provider", CredentialResolver([]))
    client: Any = boto3.Session(botocore_session=session).client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://s3.us-east-1.amazonaws.com",
        config=Config(
            signature_version=UNSIGNED,
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 0},
            proxies={},
        ),
    )
    bucket, key = "noaa-ghcn-pds", "ghcnd-version.txt"
    try:
        metadata = client.head_object(Bucket=bucket, Key=key)
        with httpx.Client(timeout=20, trust_env=False) as http:
            response = http.get(f"https://{bucket}.s3.amazonaws.com/{key}")
            response.raise_for_status()
            expected = response.content
        assert b"The current version of GHCN Daily is " in expected
        assert 0 < len(expected) <= 4096
        assert metadata["ContentLength"] == len(expected)
        assert response.headers["etag"] == metadata["ETag"]

        async def run() -> None:
            resolver = AsyncS3SourceResolver(
                client,
                namespace="public-aws-noaa-ghcn",
                max_bytes=4096,
                temp_dir=tmp_path,
            )
            async with resolver.resolve(S3Source(bucket, key)) as resource:
                assert resource.path.read_bytes() == expected
                assert (
                    resource.source.content_hash == hashlib.sha256(expected).hexdigest()
                )
                assert resource.source.locator == f"s3://{bucket}/{key}"
                assert resource.original_name == key
                assert resource.size_bytes == len(expected)
                version = json.loads(resource.source.source_version or "null")
                assert version["etag"] == metadata["ETag"]
                assert version["last_modified"] == metadata["LastModified"].isoformat()
                parsed = await AsyncTextParser().parse(
                    resource, output_dir=tmp_path / "assets"
                )
                assert parsed.report.status == "complete"
                assert project_document(parsed.document).text == expected.decode(
                    "utf-8"
                )
                path = resource.path
                source_id = resource.source.source_id
            assert not path.exists() and list(tmp_path.iterdir()) == []
            assert project_document(parsed.document).text == expected.decode("utf-8")
            async with resolver.resolve(S3Source(bucket, key)) as again:
                assert again.source.source_id == source_id
            assert list(tmp_path.iterdir()) == []
            with pytest.raises(SourceResourceLimitExceeded, match="budget"):
                async with AsyncS3SourceResolver(
                    client,
                    namespace="public-aws-noaa-ghcn",
                    max_bytes=len(expected) - 1,
                    temp_dir=tmp_path,
                ).resolve(S3Source(bucket, key)):
                    pytest.fail("oversize public S3 object delivered")
            assert list(tmp_path.iterdir()) == []

        asyncio.run(run())
        assert client.head_object(Bucket=bucket, Key=key)["ETag"] == metadata["ETag"]
    finally:
        client.close()
