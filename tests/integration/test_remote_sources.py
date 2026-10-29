from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import parse_http_list, parse_keqv_list
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore.config import Config

from comet_rag.sources import (
    AsyncS3SourceResolver,
    HttpSource,
    S3Source,
    SourceError,
    SourceResourceLimitExceeded,
)
from comet_rag.sources.http import AsyncHttpSourceResolver

pytestmark = pytest.mark.integration

_BODY = b"Actual loopback HTTP bytes\r\n  preserved\x00\n"


@pytest.fixture
def http_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/actual.txt?edition=2")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/digest-redirect":
                self.send_response(302)
                self.send_header("Location", "/digest-protected")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path in {"/digest-protected", "/digest-large"}:
                authorization = self.headers.get("Authorization", "")
                valid = False
                if authorization.startswith("Digest "):
                    values = parse_keqv_list(parse_http_list(authorization[7:]))
                    first = hashlib.sha256(b"user:example:pass").hexdigest()
                    second = hashlib.sha256(f"GET:{self.path}".encode()).hexdigest()
                    expected = hashlib.sha256(
                        f"{first}:nonce:{values.get('nc')}:{values.get('cnonce')}:auth:{second}".encode()
                    ).hexdigest()
                    valid = (
                        values.get("uri") == self.path
                        and values.get("response") == expected
                    )
                if not valid:
                    challenge = b"x" * (64 if self.path == "/digest-large" else 4)
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        'Digest realm="example", nonce="nonce", algorithm=SHA-256, qop="auth"',
                    )
                    self.send_header("Content-Length", str(len(challenge)))
                    self.end_headers()
                    self.wfile.write(challenge)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
            elif self.path == "/short":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"short")
                self.close_connection = True
            elif self.path == "/denied":
                self.send_response(302)
                self.send_header("Location", "http://blocked.invalid/no-request")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(_BODY)))
                self.send_header("ETag", '"loopback-etag"')
                self.end_headers()
                self.wfile.write(_BODY[:7])
                self.wfile.flush()
                self.wfile.write(_BODY[7:])

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def test_real_http_redirect_stream_hash_and_context_cleanup(
    http_server: str, tmp_path: Path
) -> None:
    checked: list[str] = []

    def policy(source: HttpSource) -> bool:
        checked.append(source.url)
        return source.url.startswith(f"{http_server}/")

    async def run() -> None:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            resolver = AsyncHttpSourceResolver(client, policy=policy, temp_dir=tmp_path)
            async with resolver.resolve(
                HttpSource(f"{http_server}/redirect")
            ) as resource:
                assert resource.path.read_bytes() == _BODY
                assert resource.source.content_hash == hashlib.sha256(_BODY).hexdigest()
                assert resource.size_bytes == len(_BODY)
                assert resource.original_name == "actual.txt"
                assert resource.source.locator == f"{http_server}/redirect"
                assert json.loads(resource.source.source_version or "null") == {
                    "etag": '"loopback-etag"'
                }
                path = resource.path
            assert not path.exists() and not client.is_closed
            assert list(tmp_path.iterdir()) == []
            response = await client.get(f"{http_server}/actual.txt")
            assert response.content == _BODY

    asyncio.run(run())
    assert checked == [f"{http_server}/redirect", f"{http_server}/actual.txt?edition=2"]


def test_real_http_incomplete_transfer_and_redirect_policy_cleanup(
    http_server: str, tmp_path: Path
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            resolver = AsyncHttpSourceResolver(
                client,
                policy=lambda source: source.url.startswith(f"{http_server}/"),
                temp_dir=tmp_path,
            )
            with pytest.raises(SourceError, match="stream failed"):
                async with resolver.resolve(HttpSource(f"{http_server}/short")):
                    pytest.fail("incomplete HTTP resource delivered")
            assert list(tmp_path.iterdir()) == []
            with pytest.raises(SourceError, match="policy"):
                async with resolver.resolve(HttpSource(f"{http_server}/denied")):
                    pytest.fail("denied HTTP redirect delivered")
            assert list(tmp_path.iterdir()) == []
            assert not client.is_closed

    asyncio.run(run())


def test_real_http_digest_redirect_and_challenge_budget(
    http_server: str, tmp_path: Path
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            timeout=3, trust_env=False, auth=httpx.DigestAuth("user", "pass")
        ) as client:
            resolver = AsyncHttpSourceResolver(
                client,
                policy=lambda source: source.url.startswith(f"{http_server}/"),
                max_bytes=8,
                temp_dir=tmp_path,
            )
            async with resolver.resolve(
                HttpSource(f"{http_server}/digest-redirect")
            ) as resource:
                assert resource.path.read_bytes() == b"ok"
            assert list(tmp_path.iterdir()) == []
        # 新认证实例没有缓存挑战，确保实际读取超限的 401 响应。
        async with httpx.AsyncClient(
            timeout=3, trust_env=False, auth=httpx.DigestAuth("user", "pass")
        ) as client:
            with pytest.raises(SourceResourceLimitExceeded):
                async with AsyncHttpSourceResolver(
                    client,
                    policy=lambda source: source.url.startswith(f"{http_server}/"),
                    max_bytes=8,
                    temp_dir=tmp_path,
                ).resolve(HttpSource(f"{http_server}/digest-large")):
                    pytest.fail("oversize Digest challenge delivered")
            assert not client.is_closed
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_real_s3_download_metadata_budgets_and_owned_cleanup(tmp_path: Path) -> None:
    # 专用兼容 S3 由调用方显式配置；缺后端直接失败，不把缺失当作验收通过。
    endpoint = os.environ["COMET_RAG_TEST_S3_ENDPOINT"]
    access_key = os.environ["COMET_RAG_TEST_S3_ACCESS_KEY"]
    secret_key = os.environ["COMET_RAG_TEST_S3_SECRET_KEY"]
    region = os.environ["COMET_RAG_TEST_S3_REGION"]
    client: Any = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=3,
            retries={"max_attempts": 0},
        ),
    )
    bucket = f"comet-v2-source-{uuid4().hex}"
    key = "reports/actual.txt"
    data = b"Actual S3 object bytes\r\n  text\x00"
    created = False
    uploaded = False
    try:
        client.create_bucket(Bucket=bucket)
        created = True
        response = client.put_object(
            Bucket=bucket, Key=key, Body=data, ContentType="text/plain"
        )
        uploaded = True

        async def run() -> None:
            resolver = AsyncS3SourceResolver(
                client, namespace="explicit-integration-service", temp_dir=tmp_path
            )
            async with resolver.resolve(
                S3Source(bucket, key, source_id="actual-object")
            ) as resource:
                assert resource.path.read_bytes() == data
                assert resource.source.content_hash == hashlib.sha256(data).hexdigest()
                assert resource.source.source_id == "actual-object"
                assert resource.media_type == "text/plain"
                assert resource.size_bytes == len(data)
                assert (
                    json.loads(resource.source.source_version or "null")["etag"]
                    == response["ETag"]
                )
                path = resource.path
            assert not path.exists()
            assert list(tmp_path.iterdir()) == []
            with pytest.raises(SourceError, match="budget"):
                async with AsyncS3SourceResolver(
                    client,
                    namespace="explicit-integration-service",
                    max_bytes=len(data) - 1,
                    temp_dir=tmp_path,
                ).resolve(S3Source(bucket, key)):
                    pytest.fail("oversize S3 object delivered")
            assert list(tmp_path.iterdir()) == []

        asyncio.run(run())
        assert client.head_object(Bucket=bucket, Key=key)["ContentLength"] == len(data)
    finally:
        try:
            if uploaded:
                client.delete_object(Bucket=bucket, Key=key)
            if created:
                client.delete_bucket(Bucket=bucket)
        finally:
            client.close()
